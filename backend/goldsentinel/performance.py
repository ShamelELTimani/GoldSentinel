from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean

from .database import Repository, TradeRecord


PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}


def date_range(period: str, start: datetime | None = None, end: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(timezone.utc)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if period in PERIOD_DAYS:
        return now - timedelta(days=PERIOD_DAYS[period]), now
    if period == "custom":
        return start, end
    return None, now


def _bucket(trades: list[TradeRecord], fmt: str) -> list[dict]:
    values: dict[str, float] = defaultdict(float)
    for trade in trades:
        values[trade.closed_at.strftime(fmt)] += trade.net_pnl
    return [{"period": key, "pnl": round(value, 2)} for key, value in sorted(values.items())]


def report(repository: Repository, period: str = "all", start: datetime | None = None, end: datetime | None = None, source: str | None = None) -> dict:
    start, end = date_range(period, start, end)
    trades = repository.closed_trades(start, end, source)
    equity = repository.equity(start, source)
    pnls = [trade.net_pnl for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    cumulative = 0.0
    curve = []
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        cumulative += trade.net_pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        curve.append({"timestamp": trade.closed_at, "value": round(cumulative, 2)})
    by_field = {}
    for name in ("timeframe", "setup", "session"):
        grouped: dict[str, float] = defaultdict(float)
        for trade in trades:
            grouped[getattr(trade, name)] += trade.net_pnl
        by_field[name] = [{"name": key, "pnl": round(value, 2)} for key, value in sorted(grouped.items())]
    holding = [(trade.closed_at - trade.opened_at).total_seconds() for trade in trades]
    return {
        "source": source or "ALL",
        "period": period,
        "stats": {
            "net_profit": round(sum(pnls), 2), "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "average_win": round(mean(wins), 2) if wins else 0, "average_loss": round(mean(losses), 2) if losses else 0,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
            "expectancy": round(mean(pnls), 2) if pnls else 0, "max_drawdown": round(max_drawdown, 2),
            "average_holding_seconds": round(mean(holding), 1) if holding else 0,
            "best_trade": round(max(pnls), 2) if pnls else 0, "worst_trade": round(min(pnls), 2) if pnls else 0,
            "average_achieved_rr": round(mean([t.planned_rr for t in trades]), 2) if trades else 0,
        },
        "cumulative_profit": curve,
        "equity_curve": [{"timestamp": item.timestamp, "value": item.equity} for item in equity],
        "daily": _bucket(trades, "%Y-%m-%d"), "weekly": _bucket(trades, "%G-W%V"), "monthly": _bucket(trades, "%Y-%m"),
        "breakdowns": by_field,
        "trades": [{"ticket": t.ticket, "source": t.source, "side": t.side, "volume": t.volume, "entry": t.entry, "exit": t.exit, "net_pnl": t.net_pnl, "opened_at": t.opened_at, "closed_at": t.closed_at, "timeframe": t.timeframe, "setup": t.setup, "session": t.session} for t in trades],
    }

