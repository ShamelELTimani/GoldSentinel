from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4

from .base import Broker
from ..domain import (
    TIMEFRAME_SECONDS,
    AccountSnapshot,
    BrokerResult,
    Candle,
    OrderRequest,
    Position,
    Quote,
    Side,
    Timeframe,
    utcnow,
)


class MockBroker(Broker):
    """Deterministic demo broker used by development and all automated tests."""

    def __init__(self, seed: int = 42, balance: float = 100_000) -> None:
        self._connected = False
        self._rng = random.Random(seed)
        self._balance = balance
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, dict] = {}
        self._submitted: dict[str, BrokerResult] = {}
        self._lock = Lock()
        self._price = 2687.40

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            raise ConnectionError("Mock broker is disconnected")

    def _mark(self) -> float:
        self._price += self._rng.uniform(-0.18, 0.18)
        return round(self._price, 2)

    def quote(self, symbol: str) -> Quote:
        self._require()
        mid = self._mark()
        return Quote(symbol=symbol, bid=round(mid - 0.10, 2), ask=round(mid + 0.10, 2), timestamp=utcnow(), source="MOCK")

    def account(self) -> AccountSnapshot:
        self._require()
        floating = sum(position.pnl for position in self.positions())
        margin = sum(position.volume * 100 for position in self._positions.values())
        return AccountSnapshot(balance=self._balance, equity=self._balance + floating, free_margin=self._balance + floating - margin, margin=margin, leverage=500, floating_pnl=floating, is_demo=True)

    def candles(self, symbol: str, timeframe: Timeframe, count: int, offset: int = 0) -> list[Candle]:
        self._require()
        seconds = TIMEFRAME_SECONDS[timeframe]
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=seconds * offset)
        rng = random.Random(137 + seconds + offset)
        price = 2580 + math.log10(seconds) * 22
        result: list[Candle] = []
        for index in range(count):
            ts = end - timedelta(seconds=seconds * (count - index - 1))
            drift = math.sin(index / 13) * 0.7 + rng.uniform(-1.5, 1.5)
            open_price = price
            close = max(100, open_price + drift)
            high = max(open_price, close) + rng.uniform(0.15, 1.1)
            low = min(open_price, close) - rng.uniform(0.15, 1.1)
            result.append(Candle(timestamp=ts, open=round(open_price, 2), high=round(high, 2), low=round(low, 2), close=round(close, 2), volume=rng.randint(80, 900), spread=0.2, complete=index < count - 1))
            price = close
        if result:
            self._price = result[-1].close
        return result

    def positions(self, symbol: str | None = None) -> list[Position]:
        self._require()
        quote = self.quote(symbol or "XAUUSD.m")
        items = []
        for stored in self._positions.values():
            if symbol and stored.symbol != symbol:
                continue
            current = quote.bid if stored.side == Side.buy else quote.ask
            direction = 1 if stored.side == Side.buy else -1
            pnl = (current - stored.entry) * direction * stored.volume * 100
            items.append(stored.model_copy(update={"current_price": current, "pnl": round(pnl, 2)}))
        return items

    def pending_orders(self, symbol: str | None = None) -> list[dict]:
        self._require()
        return [value for value in self._orders.values() if not symbol or value["symbol"] == symbol]

    def submit_market_order(self, symbol: str, order: OrderRequest) -> BrokerResult:
        self._require()
        with self._lock:
            if order.client_order_id in self._submitted:
                return self._submitted[order.client_order_id]
            spec = self.symbol_spec(symbol)
            if order.volume < spec["volume_min"] or order.volume > spec["volume_max"]:
                return BrokerResult(success=False, state="REJECTED", retcode=10014, message="Invalid volume")
            quote = self.quote(symbol)
            fill = quote.ask if order.side == Side.buy else quote.bid
            if order.max_price_deviation is not None and abs(fill - order.entry) > order.max_price_deviation:
                return BrokerResult(success=False, state="REJECTED", message="Executable price moved beyond the approved slippage limit")
            minimum = spec["trade_stops_level"] * spec["point"]
            if order.side == Side.buy:
                valid = order.stop_loss < fill < order.take_profit
                distances = (fill - order.stop_loss, order.take_profit - fill)
            else:
                valid = order.take_profit < fill < order.stop_loss
                distances = (order.stop_loss - fill, fill - order.take_profit)
            if not valid or min(distances) < minimum:
                return BrokerResult(success=False, state="REJECTED", message="Invalid protective levels")
            ticket = str(uuid4().int)[:10]
            result = BrokerResult(success=True, state="FILLED", retcode=10009, message="Mock fill confirmed", order_ticket=ticket, deal_ticket=ticket, position_ticket=ticket, filled_volume=order.volume)
            self._positions[ticket] = Position(ticket=ticket, symbol=symbol, side=order.side, volume=order.volume, entry=fill, current_price=fill, stop_loss=order.stop_loss, take_profit=order.take_profit, initial_risk=abs(fill - order.stop_loss) * order.volume * spec["contract_size"])
            self._submitted[order.client_order_id] = result
            return result

    def close_position(self, ticket: str, volume: float | None = None) -> BrokerResult:
        self._require()
        with self._lock:
            position = self._positions.get(ticket)
            if not position:
                return BrokerResult(success=False, state="REJECTED", message="Position not found")
            if volume and volume < position.volume:
                position.volume = round(position.volume - volume, 2)
            else:
                del self._positions[ticket]
            return BrokerResult(success=True, state="FILLED", message="Position close confirmed", position_ticket=ticket, filled_volume=volume or position.volume)

    def modify_position(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult:
        self._require()
        with self._lock:
            position = self._positions.get(ticket)
            if not position:
                return BrokerResult(success=False, state="REJECTED", message="Position not found")
            risk_widens = (position.side == Side.buy and stop_loss < position.stop_loss) or (position.side == Side.sell and stop_loss > position.stop_loss)
            if risk_widens:
                return BrokerResult(success=False, state="REJECTED", message="Stop widening requires separate risk authorization")
            position.stop_loss = stop_loss
            position.take_profit = take_profit
            return BrokerResult(success=True, state="CONFIRMED", message="Protective levels verified", position_ticket=ticket)

    def cancel_pending(self, ticket: str) -> BrokerResult:
        self._require()
        if ticket not in self._orders:
            return BrokerResult(success=False, state="REJECTED", message="Pending order not found")
        del self._orders[ticket]
        return BrokerResult(success=True, state="CANCELLED", message="Pending order cancellation confirmed", order_ticket=ticket)

    def symbol_spec(self, symbol: str) -> dict:
        return {"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01, "contract_size": 100.0, "trade_stops_level": 10, "point": 0.01, "filling_modes": ["FOK", "IOC", "RETURN"]}

