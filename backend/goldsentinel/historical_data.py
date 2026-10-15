from __future__ import annotations

from datetime import datetime, timezone

from .brokers.base import Broker
from .database import Repository
from .domain import TIMEFRAME_SECONDS, Candle, Timeframe
from .market import validate_candles


DEFAULT_HISTORY_DAYS = {
    Timeframe.m1: 14,
    Timeframe.m5: 120,
    Timeframe.m15: 180,
    Timeframe.h1: 540,
    Timeframe.h4: 1080,
}


class HistoricalDataPipeline:
    def __init__(self, broker: Broker, repository: Repository, symbol: str, batch_size: int = 5000) -> None:
        self.broker = broker
        self.repository = repository
        self.symbol = symbol
        self.batch_size = batch_size

    def refresh(self, timeframe: Timeframe, days: int | None = None) -> dict:
        days = days or DEFAULT_HISTORY_DAYS[timeframe]
        requested = max(100, int(days * 86400 / TIMEFRAME_SECONDS[timeframe]))
        collected: dict[datetime, Candle] = {}
        offset = 0
        while offset < requested:
            count = min(self.batch_size, requested - offset)
            batch = self.broker.candles(self.symbol, timeframe, count, offset)
            if not batch:
                break
            for candle in batch:
                timestamp = candle.timestamp.astimezone(timezone.utc) if candle.timestamp.tzinfo else candle.timestamp.replace(tzinfo=timezone.utc)
                collected[timestamp] = candle.model_copy(update={"timestamp": timestamp})
            if len(batch) < count:
                break
            offset += count
        rows = [collected[key] for key in sorted(collected)]
        self.repository.upsert_candles(self.symbol, timeframe, rows)
        quality = validate_candles(rows, timeframe) if len(rows) >= 2 else None
        return {"symbol": self.symbol, "timeframe": timeframe.value, "requested": requested, "stored": len(rows), "first": rows[0].timestamp if rows else None, "last": rows[-1].timestamp if rows else None, "quality": quality.__dict__ if quality else None}

    def refresh_core(self, days_by_timeframe: dict[Timeframe, int] | None = None) -> dict:
        plan = days_by_timeframe or DEFAULT_HISTORY_DAYS
        return {timeframe.value: self.refresh(timeframe, days) for timeframe, days in plan.items()}
