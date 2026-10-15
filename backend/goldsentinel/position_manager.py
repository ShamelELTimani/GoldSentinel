from __future__ import annotations

from .brokers.base import Broker
from .domain import (
    ManagementAction,
    MarketContext,
    Position,
    PositionManagementDecision,
    Quote,
    RuntimeSettings,
    Side,
    TradingMode,
    Zone,
)


class PositionManager:
    """Manages only GoldSentinel-owned demo positions under the full safety gate."""

    def __init__(self, broker: Broker, repository) -> None:
        self.broker = broker
        self.repository = repository

    @staticmethod
    def evaluate(
        position: Position,
        quote: Quote,
        context: MarketContext,
        lifecycle: dict | None = None,
        tick_size: float = 0.01,
        zones: list[Zone] | None = None,
        news: dict | None = None,
    ) -> PositionManagementDecision:
        initial_entry = float((lifecycle or {}).get("initial_entry", position.entry))
        initial_stop = float((lifecycle or {}).get("initial_stop", position.stop_loss))
        risk_distance = abs(initial_entry - initial_stop)
        current = quote.bid if position.side == Side.buy else quote.ask
        direction = 1 if position.side == Side.buy else -1
        r_multiple = ((current - initial_entry) * direction / risk_distance) if risk_distance > 0 else 0.0
        expected = "BULLISH" if position.side == Side.buy else "BEARISH"
        opposite = "BEARISH" if expected == "BULLISH" else "BULLISH"
        m5 = context.analyses.get("M5")
        m15 = context.analyses.get("M15")
        m1 = context.analyses.get("M1")
        decisive_opposition = bool(
            m5
            and m15
            and m5.bias == opposite
            and m15.bias == opposite
            and (m5.bos == opposite or m5.choch == opposite or m15.bos == opposite or m15.choch == opposite)
        )
        if decisive_opposition and r_multiple >= 0.8:
            return PositionManagementDecision(ticket=position.ticket, action=ManagementAction.close, reason="Protect profit: M5/M15 structure decisively reversed", r_multiple=round(r_multiple, 3))
        if decisive_opposition and r_multiple <= -0.35:
            return PositionManagementDecision(ticket=position.ticket, action=ManagementAction.close, reason="Cut invalidated trade: adverse M5/M15 structural break", r_multiple=round(r_multiple, 3))
        opposing_zones = [
            zone for zone in (zones or [])
            if zone.direction != position.side and not zone.broken
            and ((zone.lower > current) if position.side == Side.buy else (zone.upper < current))
        ]
        nearest_zone = min(opposing_zones, key=lambda zone: abs(zone.entry - current), default=None)
        if nearest_zone and m5 and abs(nearest_zone.entry - current) <= m5.atr * .35 and r_multiple >= .65:
            return PositionManagementDecision(ticket=position.ticket, action=ManagementAction.close, reason="Protect profit at newly confirmed opposing structure", r_multiple=round(r_multiple, 3))
        opened_at = position.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=context.timestamp.tzinfo)
        held_hours = max(0.0, (context.timestamp - opened_at).total_seconds() / 3600)
        if held_hours >= 6 and r_multiple < .15 and m5 and m5.bias == opposite and m1 and m1.bias == opposite:
            return PositionManagementDecision(ticket=position.ticket, action=ManagementAction.close, reason="Time-based exit: trade stalled while M5/M1 momentum turned adverse", r_multiple=round(r_multiple, 3))

        proposed: float | None = None
        reason = "No management threshold reached"
        if r_multiple >= 1.25:
            locked = initial_entry + direction * risk_distance * 0.25
            structural = m5.swing_low if position.side == Side.buy and m5 else m5.swing_high if m5 else None
            atr_stop = current - m5.atr * 1.2 if position.side == Side.buy and m5 else current + m5.atr * 1.2 if m5 else locked
            if position.side == Side.buy:
                proposed = max(locked, structural or locked, atr_stop, position.stop_loss)
            else:
                proposed = min(locked, structural or locked, atr_stop, position.stop_loss)
            reason = "Trail by M5 ATR/structure while locking at least 0.25R"
        elif r_multiple >= 0.8:
            proposed = initial_entry
            if position.side == Side.buy:
                proposed = max(proposed, position.stop_loss)
            else:
                proposed = min(proposed, position.stop_loss)
            reason = "Move stop to breakeven after 0.8R"
        elif news and news.get("block") and r_multiple >= .3:
            proposed = max(initial_entry, position.stop_loss) if position.side == Side.buy else min(initial_entry, position.stop_loss)
            reason = "Verified news restriction: protect the open trade at breakeven"

        if proposed is not None and abs(proposed - position.stop_loss) >= tick_size * 2:
            return PositionManagementDecision(
                ticket=position.ticket,
                action=ManagementAction.modify_stop,
                reason=reason,
                r_multiple=round(r_multiple, 3),
                new_stop_loss=proposed,
            )
        return PositionManagementDecision(ticket=position.ticket, action=ManagementAction.hold, reason=reason, r_multiple=round(r_multiple, 3))

    def manage(self, symbol: str, runtime: RuntimeSettings, context: MarketContext | None, zones: list[Zone] | None = None, news: dict | None = None) -> list[PositionManagementDecision]:
        if (
            context is None
            or runtime.mode != TradingMode.full_auto
            or not runtime.armed
            or runtime.paused
            or runtime.emergency_stop
        ):
            return []
        account = self.broker.account()
        if not account.is_demo:
            return []
        quote = self.broker.quote(symbol)
        spec = self.broker.symbol_spec(symbol)
        tick_size = float(spec.get("trade_tick_size") or spec.get("point") or 0.01)
        decisions: list[PositionManagementDecision] = []
        for position in self.broker.positions(symbol):
            if not self.broker.is_owned_position(position):
                continue
            lifecycle = self.repository.trade_lifecycle(position.ticket) if hasattr(self.repository, "trade_lifecycle") else None
            decision = self.evaluate(position, quote, context, lifecycle, tick_size, zones, news)
            decisions.append(decision)
            if decision.action == ManagementAction.hold:
                continue
            if decision.action == ManagementAction.close:
                result = self.broker.close_position(position.ticket)
                remaining = {item.ticket for item in self.broker.positions(symbol)}
                confirmed = result.success and position.ticket not in remaining
            else:
                normalized_stop, normalized_target = self.broker.normalize_protective_levels(
                    symbol, position.side, quote, float(decision.new_stop_loss), position.take_profit
                )
                # The manager may tighten risk, never widen it.
                if (position.side == Side.buy and normalized_stop < position.stop_loss) or (
                    position.side == Side.sell and normalized_stop > position.stop_loss
                ):
                    continue
                result = self.broker.modify_position(position.ticket, normalized_stop, normalized_target)
                refreshed = next((item for item in self.broker.positions(symbol) if item.ticket == position.ticket), None)
                confirmed = bool(result.success and refreshed and abs(refreshed.stop_loss - normalized_stop) <= tick_size * 1.1)
            payload = {**result.model_dump(), "reconciled": confirmed}
            if hasattr(self.repository, "record_management_action"):
                self.repository.record_management_action(position.ticket, decision.action.value, decision.reason, payload)
            if decision.action == ManagementAction.close and confirmed and hasattr(self.repository, "record_trade_outcome"):
                opened_at = position.opened_at
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=quote.timestamp.tzinfo)
                self.repository.record_trade_outcome(
                    position.ticket,
                    decision.reason,
                    payload,
                    decision.r_multiple,
                    max(0.0, (quote.timestamp - opened_at).total_seconds()),
                )
            self.repository.audit(
                "POSITION_MANAGEMENT",
                decision.reason,
                {"ticket": position.ticket, "decision": decision.model_dump(), "broker": payload},
                "INFO" if confirmed else "WARNING",
            )
        return decisions
