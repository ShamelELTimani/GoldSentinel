from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from typing import Any

from .base import Broker
from ..domain import AccountSnapshot, BrokerResult, Candle, OrderRequest, Position, Quote, Side, Timeframe


TF_NAMES = {tf: tf.value for tf in Timeframe}


class MT5Broker(Broker):
    """Thin native MetaTrader 5 adapter. The terminal owns credentials and server selection."""

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("Install the mt5 extra on Windows: pip install -e .[mt5]") from exc
        self.mt5 = mt5
        self._connected = False
        self._clock_offset_seconds = 0

    def connect(self) -> bool:
        self._connected = bool(self.mt5.initialize())
        return self._connected

    def disconnect(self) -> None:
        self.mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self.mt5.terminal_info() is not None

    def _require(self) -> None:
        if not self.is_connected() and not self.connect():
            raise ConnectionError(f"MT5 connection failed: {self.mt5.last_error()}")

    def _ensure_clock_offset(self, symbol: str) -> None:
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            return
        raw = datetime.fromtimestamp(tick.time_msc / 1000, timezone.utc)
        skew = (raw - datetime.now(timezone.utc)).total_seconds()
        rounded = round(skew / 3600) * 3600
        # Some terminals encode broker wall time as epoch seconds. Only correct a
        # near-whole-hour future skew; negative skew may simply be a stale market.
        if 30 * 60 <= skew <= 14 * 3600 and abs(skew - rounded) <= 5 * 60:
            self._clock_offset_seconds = int(rounded)
        elif abs(skew) < 30 * 60:
            self._clock_offset_seconds = 0

    def _timestamp(self, epoch_seconds: float) -> datetime:
        return datetime.fromtimestamp(epoch_seconds, timezone.utc) - timedelta(seconds=self._clock_offset_seconds)

    def account(self) -> AccountSnapshot:
        self._require()
        info = self.mt5.account_info()
        if info is None:
            raise ConnectionError(f"MT5 account unavailable: {self.mt5.last_error()}")
        demo = info.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO
        return AccountSnapshot(balance=info.balance, equity=info.equity, free_margin=info.margin_free, margin=info.margin, leverage=info.leverage, floating_pnl=info.profit, currency=info.currency, is_demo=demo)

    def quote(self, symbol: str) -> Quote:
        self._require()
        self._ensure_clock_offset(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"No quote for {symbol}: {self.mt5.last_error()}")
        return Quote(symbol=symbol, bid=tick.bid, ask=tick.ask, timestamp=self._timestamp(tick.time_msc / 1000), source="MT5")

    def candles(self, symbol: str, timeframe: Timeframe, count: int, offset: int = 0) -> list[Candle]:
        self._require()
        self._ensure_clock_offset(symbol)
        mt5_tf = getattr(self.mt5, f"TIMEFRAME_{TF_NAMES[timeframe]}")
        rates = self.mt5.copy_rates_from_pos(symbol, mt5_tf, offset, count)
        if rates is None:
            raise ValueError(f"MT5 candle retrieval failed: {self.mt5.last_error()}")
        result = []
        for index, rate in enumerate(rates):
            ts = self._timestamp(int(rate["time"]))
            # copy_rates_from_pos(..., 0, ...) includes the forming bar last.
            complete = not (offset == 0 and index == len(rates) - 1)
            result.append(Candle(timestamp=ts, open=float(rate["open"]), high=float(rate["high"]), low=float(rate["low"]), close=float(rate["close"]), volume=float(rate["tick_volume"]), spread=float(rate["spread"]), complete=complete))
        return result

    def positions(self, symbol: str | None = None) -> list[Position]:
        self._require()
        rows = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if rows is None:
            raise ConnectionError(f"Could not reconcile positions: {self.mt5.last_error()}")
        result = []
        for p in rows:
            side = Side.buy if p.type == self.mt5.POSITION_TYPE_BUY else Side.sell
            risk = self.loss_at_stop(p.symbol, side, p.volume, p.price_open, p.sl) if p.sl else 0
            result.append(Position(ticket=str(p.ticket), symbol=p.symbol, side=side, volume=p.volume, entry=p.price_open, current_price=p.price_current, stop_loss=p.sl, take_profit=p.tp, pnl=p.profit, initial_risk=risk, magic=int(getattr(p, "magic", 0)), comment=str(getattr(p, "comment", "")), opened_at=self._timestamp(p.time)))
        return result

    def pending_orders(self, symbol: str | None = None) -> list[dict]:
        self._require()
        rows = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        if rows is None:
            raise ConnectionError(f"Could not reconcile pending orders: {self.mt5.last_error()}")
        return [row._asdict() for row in rows]

    def symbol_spec(self, symbol: str) -> dict:
        self._require()
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"Symbol {symbol} is unavailable")
        modes = []
        # Python's MetaTrader5 package exposes ORDER_FILLING_* but not always
        # the SYMBOL_FILLING_* flag constants. MQL5 defines FOK=1 and IOC=2.
        if int(info.filling_mode) & 1:
            modes.append("FOK")
        if int(info.filling_mode) & 2:
            modes.append("IOC")
        # RETURN is forbidden for Market Execution symbols.
        market_execution = getattr(self.mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
        if int(info.trade_exemode) != market_execution:
            modes.append("RETURN")
        if not modes:
            raise ValueError(f"Broker did not advertise a supported filling mode for {symbol}")
        return {"volume_min": info.volume_min, "volume_max": info.volume_max, "volume_step": info.volume_step, "contract_size": info.trade_contract_size, "trade_stops_level": info.trade_stops_level, "trade_freeze_level": getattr(info, "trade_freeze_level", 0), "trade_tick_size": getattr(info, "trade_tick_size", info.point), "digits": getattr(info, "digits", 2), "point": info.point, "filling_modes": modes}

    def _round_volume(self, value: float, step: float) -> float:
        return float((Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))

    def _round_price(self, value: float, tick_size: float, upward: bool) -> float:
        tick = Decimal(str(tick_size))
        rounding = ROUND_CEILING if upward else ROUND_FLOOR
        return float((Decimal(str(value)) / tick).to_integral_value(rounding=rounding) * tick)

    def normalize_protective_levels(self, symbol: str, side: Side, quote: Quote, stop_loss: float, take_profit: float) -> tuple[float, float]:
        spec = self.symbol_spec(symbol)
        tick = float(spec["trade_tick_size"] or spec["point"])
        digits = int(spec["digits"])
        if side == Side.buy:
            stop_loss = self._round_price(stop_loss, tick, upward=False)
            take_profit = self._round_price(take_profit, tick, upward=True)
        else:
            stop_loss = self._round_price(stop_loss, tick, upward=True)
            take_profit = self._round_price(take_profit, tick, upward=False)
        return round(stop_loss, digits), round(take_profit, digits)

    def loss_at_stop(self, symbol: str, side: Side, volume: float, entry: float, stop_loss: float) -> float:
        if not stop_loss:
            return 0
        order_type = self.mt5.ORDER_TYPE_BUY if side == Side.buy else self.mt5.ORDER_TYPE_SELL
        calculator = getattr(self.mt5, "order_calc_profit", None)
        try:
            value = calculator(order_type, symbol, volume, entry, stop_loss) if calculator else None
        except Exception:
            value = None
        if value is None:
            return super().loss_at_stop(symbol, side, volume, entry, stop_loss)
        return abs(float(value))

    def submit_market_order(self, symbol: str, order: OrderRequest) -> BrokerResult:
        self._require()
        account = self.account()
        if not account.is_demo:
            return BrokerResult(success=False, state="REJECTED", message="GoldSentinel refuses non-demo MT5 order submission")
        spec = self.symbol_spec(symbol)
        volume = self._round_volume(order.volume, spec["volume_step"])
        if volume < spec["volume_min"] or volume > spec["volume_max"]:
            return BrokerResult(success=False, state="REJECTED", message="Rounded volume is outside broker limits")
        quote = self.quote(symbol)
        side_type = self.mt5.ORDER_TYPE_BUY if order.side == Side.buy else self.mt5.ORDER_TYPE_SELL
        price = quote.ask if order.side == Side.buy else quote.bid
        if order.max_price_deviation is not None and abs(price - order.entry) > order.max_price_deviation:
            return BrokerResult(success=False, state="REJECTED", message="Executable price moved beyond the approved slippage limit")
        stop_loss, take_profit = self.normalize_protective_levels(symbol, order.side, quote, order.stop_loss, order.take_profit)
        min_stop = max(float(spec["trade_stops_level"]), float(spec["trade_freeze_level"])) * float(spec["point"])
        trigger = quote.bid if order.side == Side.buy else quote.ask
        if order.side == Side.buy:
            valid_levels = stop_loss < trigger < take_profit
            stop_distance = trigger - stop_loss
            target_distance = take_profit - trigger
        else:
            valid_levels = take_profit < trigger < stop_loss
            stop_distance = stop_loss - trigger
            target_distance = trigger - take_profit
        if not valid_levels:
            return BrokerResult(success=False, state="REJECTED", message="SL/TP direction is invalid at the executable price")
        if min(stop_distance, target_distance) < min_stop:
            return BrokerResult(success=False, state="REJECTED", message="SL/TP violates broker stop-distance requirements")
        deviation = max(1, min(100, round((order.max_price_deviation or spec["point"] * 20) / spec["point"])))
        fill_map = {"FOK": self.mt5.ORDER_FILLING_FOK, "IOC": self.mt5.ORDER_FILLING_IOC, "RETURN": self.mt5.ORDER_FILLING_RETURN}
        base: dict[str, Any] = {"action": self.mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume, "type": side_type, "price": price, "sl": stop_loss, "tp": take_profit, "deviation": deviation, "magic": 260115, "comment": f"GS:{order.client_order_id[:20]}", "type_time": self.mt5.ORDER_TIME_GTC}
        last = None
        for mode in spec["filling_modes"]:
            request = {**base, "type_filling": fill_map[mode]}
            check = self.mt5.order_check(request)
            if check is None or check.retcode not in {0, getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)}:
                last = check
                continue
            result = self.mt5.order_send(request)
            last = result
            if result and result.retcode in {self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL}:
                position_ticket = str(getattr(result, "order", ""))
                confirmed = any(p.ticket == position_ticket for p in self.positions(symbol))
                return BrokerResult(success=confirmed, state="FILLED" if confirmed else "UNCERTAIN", retcode=result.retcode, message="MT5 execution reconciled" if confirmed else "Order accepted but position not yet reconciled", order_ticket=str(result.order), deal_ticket=str(result.deal), position_ticket=position_ticket if confirmed else None, filled_volume=result.volume, raw={"broker": result._asdict(), "request": request})
        return BrokerResult(success=False, state="REJECTED", retcode=getattr(last, "retcode", None), message=getattr(last, "comment", "MT5 order rejected"), raw={"broker": last._asdict() if last else {}, "request": base})

    def close_position(self, ticket: str, volume: float | None = None) -> BrokerResult:
        match = next((p for p in self.positions() if p.ticket == ticket), None)
        if not match:
            return BrokerResult(success=False, state="REJECTED", message="Position not found")
        quote = self.quote(match.symbol)
        request = OrderRequest(side=Side.sell if match.side == Side.buy else Side.buy, volume=volume or match.volume, entry=quote.bid if match.side == Side.buy else quote.ask, stop_loss=match.stop_loss, take_profit=match.take_profit)
        # position field is required to close in hedging accounts.
        spec = self.symbol_spec(match.symbol)
        side_type = self.mt5.ORDER_TYPE_SELL if match.side == Side.buy else self.mt5.ORDER_TYPE_BUY
        payload = {"action": self.mt5.TRADE_ACTION_DEAL, "position": int(ticket), "symbol": match.symbol, "volume": request.volume, "type": side_type, "price": request.entry, "deviation": 20, "magic": 260115, "comment": "GoldSentinel close", "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": getattr(self.mt5, f"ORDER_FILLING_{spec['filling_modes'][0]}")}
        result = self.mt5.order_send(payload)
        ok = bool(result and result.retcode in {self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL})
        deal_payload = {}
        if ok and getattr(result, "deal", None):
            history = getattr(self.mt5, "history_deals_get", None)
            try:
                deals = history(ticket=int(result.deal)) if history else None
                if deals:
                    deal_payload = deals[-1]._asdict()
            except Exception:
                deal_payload = {}
        return BrokerResult(success=ok, state="FILLED" if ok else "REJECTED", retcode=getattr(result, "retcode", None), message=getattr(result, "comment", "No response"), position_ticket=ticket, filled_volume=getattr(result, "volume", 0), raw={"broker": result._asdict() if result else {}, "request": payload, "deal": deal_payload})

    def modify_position(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult:
        match = next((p for p in self.positions() if p.ticket == ticket), None)
        if not match:
            return BrokerResult(success=False, state="REJECTED", message="Position not found")
        widens = (match.side == Side.buy and stop_loss < match.stop_loss) or (match.side == Side.sell and stop_loss > match.stop_loss)
        if widens:
            return BrokerResult(success=False, state="REJECTED", message="Stop widening requires separate risk authorization")
        quote = self.quote(match.symbol)
        stop_loss, take_profit = self.normalize_protective_levels(match.symbol, match.side, quote, stop_loss, take_profit)
        spec = self.symbol_spec(match.symbol)
        minimum = max(float(spec["trade_stops_level"]), float(spec["trade_freeze_level"])) * float(spec["point"])
        trigger = quote.bid if match.side == Side.buy else quote.ask
        distance = trigger - stop_loss if match.side == Side.buy else stop_loss - trigger
        if distance < minimum:
            return BrokerResult(success=False, state="REJECTED", message="Requested stop is inside the broker stop/freeze distance")
        payload = {"action": self.mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": match.symbol, "sl": stop_loss, "tp": take_profit}
        result = self.mt5.order_send(payload)
        ok = bool(result and result.retcode == self.mt5.TRADE_RETCODE_DONE)
        return BrokerResult(success=ok, state="CONFIRMED" if ok else "REJECTED", retcode=getattr(result, "retcode", None), message=getattr(result, "comment", "No response"), position_ticket=ticket, raw={"broker": result._asdict() if result else {}, "request": payload})

    def cancel_pending(self, ticket: str) -> BrokerResult:
        result = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
        ok = bool(result and result.retcode == self.mt5.TRADE_RETCODE_DONE)
        return BrokerResult(success=ok, state="CANCELLED" if ok else "REJECTED", retcode=getattr(result, "retcode", None), message=getattr(result, "comment", "No response"), order_ticket=ticket)

