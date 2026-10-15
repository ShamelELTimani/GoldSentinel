from __future__ import annotations

from dataclasses import dataclass

from .candle_analysis import CandleAnalysisEngine
from .domain import Candle, Side, StrategyProfile, Timeframe
from .zones import detect_zones


def _session(timestamp) -> str:
    hour = timestamp.hour
    if 7 <= hour < 12:
        return "LONDON"
    if 12 <= hour < 17:
        return "NEW_YORK_OVERLAP"
    if 17 <= hour < 21:
        return "NEW_YORK"
    return "ASIA_OTHER"


@dataclass
class BacktestConfig:
    initial_balance: float = 100_000
    max_risk_pct: float = 2
    spread: float = 0.2
    commission_per_lot: float = 7
    slippage: float = 0.1
    warmup: int = 80


def run_backtest(candles: list[Candle], timeframe: Timeframe, profile: StrategyProfile, config: BacktestConfig | None = None) -> dict:
    """Completed-bar replay using the live candle-analysis engine; stop wins ambiguous bars."""
    config = config or BacktestConfig()
    balance = config.initial_balance
    peak = balance
    max_drawdown = 0.0
    trades: list[dict] = []
    thresholds = {StrategyProfile.conservative: 72, StrategyProfile.balanced: 58, StrategyProfile.opportunistic: 48}
    analyzer = CandleAnalysisEngine()
    index = config.warmup
    while index < len(candles) - 1:
        history = [c.model_copy(update={"complete": True}) for c in candles[:index]]
        analysis = analyzer.analyze(history, timeframe)
        zones = detect_zones(history, timeframe, 5)
        next_bar = candles[index]
        previous_close = history[-1].close
        nearby = [item for item in zones if item.quality_score >= thresholds[profile] and max(0.0, previous_close - item.upper, item.lower - previous_close) <= analysis.atr * .65]
        zone = max(nearby, key=lambda item: item.quality_score, default=None)
        if zone:
            side = zone.direction
            stop_loss = zone.stop_loss
            take_profit = zone.take_profit
            quality = zone.quality_score
            setup_type = "ZONE_REVERSAL"
        elif analysis.bias in {"BULLISH", "BEARISH"} and analysis.momentum == analysis.bias and analysis.evidence_score >= thresholds[profile]:
            side = Side.buy if analysis.bias == "BULLISH" else Side.sell
            stop_loss = analysis.swing_low if side == Side.buy else analysis.swing_high
            fallback = previous_close - analysis.atr * 1.2 if side == Side.buy else previous_close + analysis.atr * 1.2
            stop_loss = min(stop_loss or fallback, fallback) if side == Side.buy else max(stop_loss or fallback, fallback)
            risk = abs(previous_close - stop_loss)
            take_profit = previous_close + risk * 1.8 if side == Side.buy else previous_close - risk * 1.8
            quality = analysis.evidence_score
            setup_type = "BREAKOUT_RETEST" if analysis.bos else "TREND_CONTINUATION"
        else:
            index += 1
            continue
        entry = next_bar.open + (config.spread / 2 + config.slippage) * (1 if side == Side.buy else -1)
        if (side == Side.buy and not stop_loss < entry < take_profit) or (side == Side.sell and not take_profit < entry < stop_loss):
            index += 1
            continue
        risk_amount = balance * config.max_risk_pct / 100 * min(1, quality / 100)
        stop_distance = abs(entry - stop_loss)
        volume = max(0.01, round(risk_amount / max(stop_distance * 100, 0.01), 2))
        outcome = None
        exit_price = entry
        exit_index = index
        active_stop = stop_loss
        exit_reason = "TIMEOUT"
        for cursor in range(index, min(len(candles), index + 100)):
            bar = candles[cursor]
            stop_hit = bar.low <= active_stop if side == Side.buy else bar.high >= active_stop
            target_hit = bar.high >= take_profit if side == Side.buy else bar.low <= take_profit
            if stop_hit:  # Conservative handling when both occur in one bar.
                outcome, exit_price, exit_index, exit_reason = "LOSS", active_stop, cursor, "STOP"
                break
            if target_hit:
                outcome, exit_price, exit_index, exit_reason = "WIN", take_profit, cursor, "TARGET"
                break
            closed_history = [item.model_copy(update={"complete": True}) for item in candles[: cursor + 1]]
            if len(closed_history) >= 55:
                current_analysis = analyzer.analyze(closed_history, timeframe)
                favorable = ((bar.close - entry) if side == Side.buy else (entry - bar.close)) / max(stop_distance, 1e-9)
                opposite = "BEARISH" if side == Side.buy else "BULLISH"
                if favorable >= .8:
                    active_stop = max(active_stop, entry) if side == Side.buy else min(active_stop, entry)
                if favorable >= 1.25:
                    structural = current_analysis.swing_low if side == Side.buy else current_analysis.swing_high
                    if structural:
                        active_stop = max(active_stop, structural, entry + stop_distance * .25) if side == Side.buy else min(active_stop, structural, entry - stop_distance * .25)
                if current_analysis.bias == opposite and (current_analysis.bos == opposite or current_analysis.choch == opposite) and cursor + 1 < len(candles):
                    outcome, exit_price, exit_index, exit_reason = "EARLY", candles[cursor + 1].open, cursor + 1, "STRUCTURE_REVERSAL"
                    break
        if outcome:
            direction = 1 if side == Side.buy else -1
            gross = (exit_price - entry) * direction * volume * 100
            costs = config.commission_per_lot * volume
            pnl = gross - costs
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, peak - balance)
            trades.append({"entry_time": next_bar.timestamp, "exit_time": candles[exit_index].timestamp, "side": side.value, "entry": entry, "exit": exit_price, "volume": volume, "pnl": round(pnl, 2), "outcome": outcome, "quality": quality, "setup_type": setup_type, "session": _session(next_bar.timestamp), "exit_reason": exit_reason, "r_multiple": round((exit_price-entry)*direction/max(stop_distance, 1e-9), 3)})
            index = exit_index + 1
        else:
            index += 1
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    breakdown = {name: {"trades": len(rows), "net_profit": round(sum(item["pnl"] for item in rows), 2)} for name in sorted({item["setup_type"] for item in trades}) for rows in [[item for item in trades if item["setup_type"] == name]]}
    session_breakdown = {name: {"trades": len(rows), "net_profit": round(sum(item["pnl"] for item in rows), 2)} for name in sorted({item["session"] for item in trades}) for rows in [[item for item in trades if item["session"] == name]]}
    return {"profile": profile.value, "initial_balance": config.initial_balance, "final_balance": round(balance, 2), "net_profit": round(balance - config.initial_balance, 2), "max_drawdown": round(max_drawdown, 2), "total_trades": len(trades), "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0, "profit_factor": round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 3) if losses else None, "average_r": round(sum(item["r_multiple"] for item in trades) / len(trades), 3) if trades else 0, "setup_breakdown": breakdown, "session_breakdown": session_breakdown, "early_exits": sum(item["exit_reason"] == "STRUCTURE_REVERSAL" for item in trades), "statistically_limited": len(trades) < 100, "trades": trades}


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

