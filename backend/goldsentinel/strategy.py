from __future__ import annotations

from datetime import timedelta

from .domain import (
    DecisionAction,
    DecisionClass,
    Quote,
    RiskRequest,
    Side,
    StrategyProfile,
    Timeframe,
    TradeCandidate,
    TradeSetup,
    Zone,
    utcnow,
)
from .market import indicators
from .risk import RiskManager
from .zones import structure_summary


PROFILE_MIN_SCORE = {
    StrategyProfile.conservative: 72,
    StrategyProfile.balanced: 60,
    StrategyProfile.opportunistic: 50,
}
PROFILE_MAX_DISTANCE_ATR = {
    StrategyProfile.conservative: 0.40,
    StrategyProfile.balanced: 0.75,
    StrategyProfile.opportunistic: 1.05,
}


class StrategyEngine:
    def __init__(self, risk_manager: RiskManager) -> None:
        self.risk_manager = risk_manager

    def evaluate(
        self,
        symbol,
        timeframe: Timeframe,
        candles,
        zones: list[Zone],
        quote: Quote,
        account,
        positions,
        settings,
        profile: StrategyProfile,
        spec,
        news: dict,
    ) -> TradeCandidate:
        now = utcnow()
        metric = indicators(candles)
        structure = structure_summary(candles)
        atr = max(metric["latest"]["atr"], float(spec.get("point", 0.01)) * 10, 0.01)
        price = (quote.bid + quote.ask) / 2
        active = [zone for zone in zones if not zone.broken]
        rationale = [
            f"{timeframe.value} structure is {structure['trend']}",
            f"RSI {metric['latest']['rsi']:.1f}",
            f"Volatility regime {metric['latest']['volatility_regime']}",
            news["reason"],
        ]
        if news["block"]:
            return self._no_trade(
                symbol,
                timeframe,
                price,
                rationale + ["News policy blocks new entries"],
                DecisionClass.operational,
                [news["reason"]],
                evaluated_zones=len(active),
            )
        if not active:
            return self._no_trade(
                symbol,
                timeframe,
                price,
                rationale + ["No active support or resistance zone is available"],
            )

        accepted: list[tuple[float, Zone, float, float, object, list[str]]] = []
        rejected: list[str] = []
        risk_rejections = 0
        min_stop_distance = float(spec.get("trade_stops_level", 0)) * float(
            spec.get("point", 0.01)
        )
        max_distance = PROFILE_MAX_DISTANCE_ATR[profile]

        for zone in active:
            entry = quote.ask if zone.direction == Side.buy else quote.bid
            side_name = zone.direction.value
            if zone.direction == Side.buy:
                distance = max(0.0, entry - zone.upper, zone.lower - entry)
                direction_valid = zone.stop_loss < entry < zone.take_profit
                stop_distance = entry - zone.stop_loss
                target_distance = zone.take_profit - entry
            else:
                distance = max(0.0, zone.lower - entry, entry - zone.upper)
                direction_valid = zone.take_profit < entry < zone.stop_loss
                stop_distance = zone.stop_loss - entry
                target_distance = entry - zone.take_profit
            distance_atr = distance / atr

            if not direction_valid:
                rejected.append(f"{side_name} {zone.id}: executable price is beyond its protective levels")
                continue
            if min(stop_distance, target_distance) < min_stop_distance:
                rejected.append(f"{side_name} {zone.id}: broker minimum stop distance is not met")
                continue
            strong_early_entry = zone.quality_score >= 78 and distance_atr <= max_distance + 0.20
            if distance_atr > max_distance and not strong_early_entry:
                rejected.append(
                    f"{side_name} {zone.id}: {distance_atr:.2f} ATR away exceeds the {profile.value.lower()} entry window"
                )
                continue

            expected_trend = "BULLISH" if zone.direction == Side.buy else "BEARISH"
            aligned = structure["trend"] == expected_trend
            structure_bonus = 8 if aligned else -4 if structure["trend"] in {"BULLISH", "BEARISH"} else 0
            setup_label = "trend pullback/continuation" if aligned else "counter-trend reaction"
            if structure["trend"] == "RANGING":
                boundary = structure.get("previous_low") if zone.direction == Side.buy else structure.get("previous_high")
                at_boundary = boundary is not None and abs(((zone.lower + zone.upper) / 2) - boundary) <= atr
                structure_bonus = 6 if at_boundary else 1
                setup_label = "range-boundary reversal" if at_boundary else "range reaction"

            reaction_bonus = 4 if zone.touches == 1 else 2 if zone.touches in {0, 2} else -min(8, (zone.touches - 2) * 2)
            freshness_bonus = zone.relevance * 5
            conflicts = [
                other
                for other in active
                if other.direction != zone.direction
                and not (
                    other.upper < zone.lower - atr * 0.25
                    or other.lower > zone.upper + atr * 0.25
                )
            ]
            conflict_penalty = min(14, len(conflicts) * 7)
            distance_penalty = min(12, distance_atr * 8)
            score = (
                zone.quality_score
                + structure_bonus
                + reaction_bonus
                + freshness_bonus
                - conflict_penalty
                - distance_penalty
            )
            if structure.get("liquidity_sweep"):
                score += 3
            if score < PROFILE_MIN_SCORE[profile]:
                rejected.append(
                    f"{side_name} {zone.id}: adjusted quality {score:.1f} is below the {profile.value.lower()} threshold"
                )
                continue

            rr = target_distance / max(stop_distance, 1e-9)
            if rr < settings.preferred_min_rr:
                rejected.append(
                    f"{side_name} {zone.id}: executable R:R {rr:.2f} is below {settings.preferred_min_rr:.2f}"
                )
                continue

            risk = self.risk_manager.calculate(
                RiskRequest(
                    side=zone.direction,
                    entry=entry,
                    stop_loss=zone.stop_loss,
                    quality_score=min(score, 100),
                    volatility_factor=(
                        1.3 if metric["latest"]["volatility_regime"] == "HIGH" else 1
                    ),
                    news_factor=news["factor"],
                ),
                account,
                settings,
                profile,
                spec,
                positions,
            )
            if not risk.allowed:
                risk_rejections += 1
                rejected.append(f"{side_name} {zone.id}: {risk.reason}")
                continue

            rank = score + min(rr, 3) * 2 - distance_atr * 3
            reasons = [
                f"Selected {side_name} {setup_label} from {len(active)} active zones",
                f"Executable {'Ask' if zone.direction == Side.buy else 'Bid'} {entry:.2f}; zone is {distance_atr:.2f} ATR away",
                f"Adjusted quality {score:.1f}; freshness {zone.relevance:.2f}; {zone.touches} prior reaction(s)",
                f"Conflicting nearby zones: {len(conflicts)}",
                *zone.evidence,
                risk.reason,
            ]
            accepted.append((rank, zone, entry, rr, risk, reasons))

        if not accepted:
            decision_class = (
                DecisionClass.risk if risk_rejections and risk_rejections == len(rejected) else DecisionClass.market
            )
            summary = rejected[:6]
            if len(rejected) > 6:
                summary.append(f"{len(rejected) - 6} additional zone rejection(s)")
            return self._no_trade(
                symbol,
                timeframe,
                price,
                rationale + [f"Evaluated {len(active)} active zone(s); none qualified", *summary],
                decision_class,
                summary if decision_class == DecisionClass.risk else [],
                evaluated_zones=len(active),
            )

        selected_rank, zone, entry, rr, risk, reasons = max(accepted, key=lambda item: item[0])
        max_price_deviation = max(
            quote.spread * 2,
            atr * 0.15,
            min_stop_distance,
            float(spec.get("point", 0.01)) * 20,
        )
        return TradeCandidate(
            action=DecisionAction(zone.direction.value),
            symbol=symbol,
            timeframe=timeframe,
            entry=round(entry, 5),
            stop_loss=zone.stop_loss,
            take_profit=zone.take_profit,
            volume=risk.volume,
            risk_amount=risk.risk_amount,
            risk_pct=risk.risk_pct,
            reward_risk=round(rr, 2),
            quality_score=round(min(100, max(0, selected_rank)), 1),
            zone_id=zone.id,
            news_status=news["status"],
            rationale=rationale + reasons,
            spread_at_signal=round(quote.spread, 5),
            max_price_deviation=round(max_price_deviation, 5),
            evaluated_zones=len(active),
            expires_at=now + timedelta(minutes=15),
        )

    def _no_trade(
        self,
        symbol: str,
        timeframe: Timeframe,
        price: float,
        rationale: list[str],
        decision_class: DecisionClass = DecisionClass.market,
        blockers: list[str] | None = None,
        evaluated_zones: int = 0,
    ) -> TradeCandidate:
        return TradeCandidate(
            action=DecisionAction.no_trade,
            symbol=symbol,
            timeframe=timeframe,
            entry=price,
            stop_loss=price,
            take_profit=price,
            volume=0,
            risk_amount=0,
            risk_pct=0,
            reward_risk=0,
            quality_score=0,
            news_status=(
                "BLOCKED"
                if decision_class == DecisionClass.operational
                and any("news" in item.lower() or "calendar" in item.lower() for item in rationale)
                else "NORMAL"
            ),
            rationale=rationale,
            decision_class=decision_class,
            blockers=blockers or [],
            evaluated_zones=evaluated_zones,
            expires_at=utcnow() + timedelta(minutes=5),
            status=(
                "BLOCKED"
                if decision_class in {DecisionClass.operational, DecisionClass.risk}
                else "REJECTED"
            ),
        )

    def evaluate_setup(
        self,
        symbol: str,
        setup: TradeSetup,
        quote: Quote,
        account,
        positions,
        settings,
        profile: StrategyProfile,
        spec: dict,
        news: dict,
        context: dict,
    ) -> TradeCandidate:
        entry = quote.ask if setup.side == Side.buy else quote.bid
        rationale = [
            f"{setup.setup_type.value.replace('_', ' ').title()} {'inside a zone' if setup.in_zone else 'between zones'}",
            setup.trigger,
            *setup.reasons,
            news["reason"],
        ]
        if news["block"]:
            return self._no_trade(
                symbol,
                setup.timeframe,
                entry,
                rationale + ["News policy blocks this otherwise qualified setup"],
                DecisionClass.operational,
                [news["reason"]],
            )
        stop_distance = (entry - setup.stop_loss) if setup.side == Side.buy else (setup.stop_loss - entry)
        target_distance = (setup.take_profit - entry) if setup.side == Side.buy else (entry - setup.take_profit)
        min_stop = max(float(spec.get("trade_stops_level", 0)), float(spec.get("trade_freeze_level", 0))) * float(spec.get("point", 0.01))
        if stop_distance <= 0 or target_distance <= 0 or min(stop_distance, target_distance) < min_stop:
            return self._no_trade(symbol, setup.timeframe, entry, rationale + ["Executable quote invalidates protective levels"], evaluated_zones=0)
        rr = target_distance / stop_distance
        if rr < settings.preferred_min_rr:
            return self._no_trade(symbol, setup.timeframe, entry, rationale + [f"Executable R:R {rr:.2f} is below {settings.preferred_min_rr:.2f}"], evaluated_zones=0)
        risk = self.risk_manager.calculate(
            RiskRequest(
                side=setup.side,
                entry=entry,
                stop_loss=setup.stop_loss,
                quality_score=setup.evidence_score,
                news_factor=news["factor"],
                setup_factor=0.9 if setup.setup_type.value == "TREND_CONTINUATION" else 1,
            ),
            account,
            settings,
            profile,
            spec,
            positions,
            float(spec.get("daily_drawdown_pct", 0)),
            int(spec.get("consecutive_losses", 0)),
        )
        if not risk.allowed:
            return self._no_trade(symbol, setup.timeframe, entry, rationale + [risk.reason], DecisionClass.risk, [risk.reason])
        return TradeCandidate(
            action=DecisionAction(setup.side.value),
            symbol=symbol,
            timeframe=setup.timeframe,
            entry=round(entry, 5),
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            volume=risk.volume,
            risk_amount=risk.risk_amount,
            risk_pct=risk.risk_pct,
            reward_risk=round(rr, 2),
            quality_score=setup.evidence_score,
            news_status=news["status"],
            rationale=rationale + [risk.reason],
            spread_at_signal=round(quote.spread, 5),
            max_price_deviation=round(max(quote.spread * 2, float(spec.get("trade_tick_size", spec.get("point", 0.01))) * 20), 5),
            setup_type=setup.setup_type,
            setup_fingerprint=setup.fingerprint,
            entry_trigger=setup.trigger,
            in_zone=setup.in_zone,
            timeframe_context=context,
            expires_at=setup.expires_at,
        )
