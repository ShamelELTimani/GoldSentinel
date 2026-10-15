from __future__ import annotations

from datetime import timedelta

from .domain import DecisionAction, DecisionClass, RiskRequest, Side, StrategyProfile, Timeframe, TradeCandidate, Zone, utcnow
from .market import indicators
from .risk import RiskManager
from .zones import structure_summary


PROFILE_MIN_SCORE = {StrategyProfile.conservative: 72, StrategyProfile.balanced: 60, StrategyProfile.opportunistic: 48}


class StrategyEngine:
    def __init__(self, risk_manager: RiskManager) -> None:
        self.risk_manager = risk_manager

    def evaluate(self, symbol, timeframe: Timeframe, candles, zones: list[Zone], account, positions, settings, profile: StrategyProfile, spec, news: dict) -> TradeCandidate:
        now = utcnow()
        metric = indicators(candles)
        structure = structure_summary(candles)
        price = candles[-1].close
        candidates = sorted((z for z in zones if not z.broken), key=lambda z: abs((z.lower + z.upper) / 2 - price))
        rationale = [f"{timeframe.value} structure is {structure['trend']}", f"RSI {metric['latest']['rsi']:.1f}", f"Volatility regime {metric['latest']['volatility_regime']}", news["reason"]]
        if news["block"]:
            return self._no_trade(symbol, timeframe, price, rationale + ["News policy blocks new entries"], DecisionClass.operational, [news["reason"]])
        if not candidates:
            return self._no_trade(symbol, timeframe, price, rationale + ["No active zone is available"], DecisionClass.market)
        zone = candidates[0]
        distance_atr = abs(price - zone.entry) / max(metric["latest"]["atr"], 0.01)
        aligned = structure["trend"] == ("BULLISH" if zone.direction == Side.buy else "BEARISH")
        score = zone.quality_score + (8 if aligned else -4) - min(distance_atr * 5, 20)
        rationale.extend(zone.evidence)
        rationale.append(f"Zone is {distance_atr:.2f} ATR from price")
        if score < PROFILE_MIN_SCORE[profile]:
            return self._no_trade(symbol, timeframe, price, rationale + [f"Adjusted quality {score:.1f} is below {profile.value.lower()} threshold"], DecisionClass.market)
        rr = abs(zone.take_profit - zone.entry) / max(abs(zone.entry - zone.stop_loss), 1e-9)
        if rr < settings.preferred_min_rr:
            return self._no_trade(symbol, timeframe, price, rationale + ["Reward-to-risk is below the configured preference and has no approved evidence exception"], DecisionClass.market)
        risk = self.risk_manager.calculate(RiskRequest(side=zone.direction, entry=zone.entry, stop_loss=zone.stop_loss, quality_score=min(score, 100), volatility_factor=1.3 if metric["latest"]["volatility_regime"] == "HIGH" else 1, news_factor=news["factor"]), account, settings, profile, spec, positions)
        if not risk.allowed:
            return self._no_trade(symbol, timeframe, price, rationale + [risk.reason], DecisionClass.risk, [risk.reason])
        return TradeCandidate(action=DecisionAction(zone.direction.value), symbol=symbol, timeframe=timeframe, entry=zone.entry, stop_loss=zone.stop_loss, take_profit=zone.take_profit, volume=risk.volume, risk_amount=risk.risk_amount, risk_pct=risk.risk_pct, reward_risk=round(rr, 2), quality_score=round(score, 1), zone_id=zone.id, news_status=news["status"], rationale=rationale + [risk.reason], expires_at=now + timedelta(minutes=15))

    def _no_trade(self, symbol: str, timeframe: Timeframe, price: float, rationale: list[str], decision_class: DecisionClass = DecisionClass.market, blockers: list[str] | None = None) -> TradeCandidate:
        return TradeCandidate(action=DecisionAction.no_trade, symbol=symbol, timeframe=timeframe, entry=price, stop_loss=price, take_profit=price, volume=0, risk_amount=0, risk_pct=0, reward_risk=0, quality_score=0, news_status="BLOCKED" if decision_class == DecisionClass.operational and any("news" in item.lower() or "calendar" in item.lower() for item in rationale) else "NORMAL", rationale=rationale, decision_class=decision_class, blockers=blockers or [], expires_at=utcnow() + timedelta(minutes=5), status="BLOCKED" if decision_class in {DecisionClass.operational, DecisionClass.risk} else "REJECTED")

