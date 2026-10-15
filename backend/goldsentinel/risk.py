from __future__ import annotations

import math

from .domain import AccountSnapshot, Position, RiskRequest, RiskResult, RiskSettings, Side, StrategyProfile


PROFILE_FACTOR = {
    StrategyProfile.conservative: 0.70,
    StrategyProfile.balanced: 0.88,
    StrategyProfile.opportunistic: 1.0,
}


class RiskManager:
    def calculate(self, request: RiskRequest, account: AccountSnapshot, settings: RiskSettings, profile: StrategyProfile, spec: dict, positions: list[Position], daily_drawdown_pct: float = 0, consecutive_losses: int = 0) -> RiskResult:
        if account.equity <= 0:
            return RiskResult(allowed=False, reason="Account equity must be positive")
        if request.side == Side.buy and request.stop_loss >= request.entry:
            return RiskResult(allowed=False, reason="BUY stop loss must be below executable entry")
        if request.side == Side.sell and request.stop_loss <= request.entry:
            return RiskResult(allowed=False, reason="SELL stop loss must be above executable entry")
        if daily_drawdown_pct >= settings.daily_drawdown_stop_pct:
            return RiskResult(allowed=False, reason="Daily equity drawdown circuit breaker is active")
        if consecutive_losses >= settings.consecutive_loss_limit:
            return RiskResult(allowed=False, reason="Consecutive-loss cooldown is active")
        if len(positions) >= settings.max_positions:
            return RiskResult(allowed=False, reason="Maximum concurrent positions reached")
        if request.quality_score < 35:
            return RiskResult(allowed=False, reason="Setup quality is below the minimum observable-evidence threshold")
        open_risk = sum(max(position.initial_risk, 0) for position in positions)
        open_pct = open_risk / account.equity * 100 if account.equity > 0 else 100
        remaining = max(0, settings.max_combined_risk_pct - open_pct)
        if remaining <= 0:
            return RiskResult(allowed=False, reason="Combined open-risk limit reached")
        quality_factor = 0.25 + (request.quality_score / 100) * 0.75
        volatility_penalty = min(1, 1 / max(request.volatility_factor, 1))
        adaptive = settings.max_risk_per_trade_pct * quality_factor * PROFILE_FACTOR[profile] * volatility_penalty * request.news_factor * request.performance_factor * request.setup_factor
        risk_pct = min(settings.max_risk_per_trade_pct, remaining, adaptive)
        if risk_pct < 0.05:
            return RiskResult(allowed=False, reason="Adaptive risk budget is too small under current conditions", remaining_risk_pct=remaining)
        amount = account.equity * risk_pct / 100
        stop_distance = abs(request.entry - request.stop_loss)
        loss_per_lot = stop_distance * spec["contract_size"]
        raw_volume = amount / loss_per_lot if loss_per_lot else 0
        step = float(spec["volume_step"])
        if step <= 0 or float(spec["contract_size"]) <= 0:
            return RiskResult(allowed=False, reason="Broker volume or contract specification is invalid")
        volume = math.floor(raw_volume / step + 1e-9) * step
        volume = round(min(volume, spec["volume_max"]), 8)
        if volume < spec["volume_min"]:
            return RiskResult(allowed=False, reason="Broker minimum volume would exceed the permitted risk", risk_pct=risk_pct, risk_amount=amount, stop_distance=stop_distance, remaining_risk_pct=remaining)
        actual_amount = volume * loss_per_lot
        actual_pct = actual_amount / account.equity * 100
        if actual_pct > settings.max_risk_per_trade_pct + 1e-9:
            return RiskResult(allowed=False, reason="Rounded order volume exceeds per-trade risk cap")
        return RiskResult(allowed=True, reason="Risk fits per-trade, combined exposure, volume, and circuit-breaker limits", risk_pct=round(actual_pct, 4), risk_amount=round(actual_amount, 2), volume=volume, stop_distance=round(stop_distance, 5), remaining_risk_pct=round(max(0, remaining - actual_pct), 4))

