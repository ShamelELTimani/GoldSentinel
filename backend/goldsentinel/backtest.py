from __future__ import annotations

from dataclasses import dataclass

from .domain import Candle, Side, StrategyProfile, Timeframe
from .zones import detect_zones


@dataclass
class BacktestConfig:
    initial_balance: float = 100_000
    max_risk_pct: float = 2
    spread: float = 0.2
    commission_per_lot: float = 7
    slippage: float = 0.1
    warmup: int = 80


def run_backtest(candles: list[Candle], timeframe: Timeframe, profile: StrategyProfile, config: BacktestConfig | None = None) -> dict:
    """Bar-by-bar replay. Signals use only prior completed candles; stop wins ambiguous bars."""
    config = config or BacktestConfig()
    balance = config.initial_balance
    peak = balance
    max_drawdown = 0.0
    trades: list[dict] = []
    thresholds = {StrategyProfile.conservative: 75, StrategyProfile.balanced: 65, StrategyProfile.opportunistic: 55}
    index = config.warmup
    while index < len(candles) - 1:
        history = [c.model_copy(update={"complete": True}) for c in candles[:index]]
        zones = detect_zones(history, timeframe, 5)
        next_bar = candles[index]
        zone = next((item for item in zones if item.quality_score >= thresholds[profile] and item.lower <= next_bar.high and item.upper >= next_bar.low), None)
        if not zone:
            index += 1
            continue
        risk_amount = balance * config.max_risk_pct / 100 * min(1, zone.quality_score / 100)
        stop_distance = abs(zone.entry - zone.stop_loss)
        volume = max(0.01, round(risk_amount / max(stop_distance * 100, 0.01), 2))
        entry = zone.entry + (config.spread / 2 + config.slippage) * (1 if zone.direction == Side.buy else -1)
        outcome = None
        exit_price = entry
        exit_index = index
        for cursor in range(index, min(len(candles), index + 100)):
            bar = candles[cursor]
            stop_hit = bar.low <= zone.stop_loss if zone.direction == Side.buy else bar.high >= zone.stop_loss
            target_hit = bar.high >= zone.take_profit if zone.direction == Side.buy else bar.low <= zone.take_profit
            if stop_hit:  # Conservative handling when both occur in one bar.
                outcome, exit_price, exit_index = "LOSS", zone.stop_loss, cursor
                break
            if target_hit:
                outcome, exit_price, exit_index = "WIN", zone.take_profit, cursor
                break
        if outcome:
            direction = 1 if zone.direction == Side.buy else -1
            gross = (exit_price - entry) * direction * volume * 100
            costs = config.commission_per_lot * volume
            pnl = gross - costs
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, peak - balance)
            trades.append({"entry_time": next_bar.timestamp, "exit_time": candles[exit_index].timestamp, "side": zone.direction.value, "entry": entry, "exit": exit_price, "volume": volume, "pnl": round(pnl, 2), "outcome": outcome, "quality": zone.quality_score})
            index = exit_index + 1
        else:
            index += 1
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    return {"profile": profile.value, "initial_balance": config.initial_balance, "final_balance": round(balance, 2), "net_profit": round(balance - config.initial_balance, 2), "max_drawdown": round(max_drawdown, 2), "total_trades": len(trades), "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0, "profit_factor": round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 3) if losses else None, "statistically_limited": len(trades) < 100, "trades": trades}


def walk_forward(candles: list[Candle], timeframe: Timeframe, profile: StrategyProfile, folds: int = 4) -> dict:
    fold_size = len(candles) // folds
    results = []
    for fold in range(1, folds):
        test = candles[max(0, fold * fold_size - 100):(fold + 1) * fold_size]
        result = run_backtest(test, timeframe, profile)
        result["fold"] = fold
        result["out_of_sample"] = True
        results.append(result)
    return {"profile": profile.value, "folds": results, "net_profit": round(sum(r["net_profit"] for r in results), 2), "total_trades": sum(r["total_trades"] for r in results), "statistically_limited": sum(r["total_trades"] for r in results) < 100}

