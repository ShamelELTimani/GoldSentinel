from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .domain import TIMEFRAME_SECONDS, Candle, Timeframe


@dataclass
class DataQuality:
    valid: bool
    stale: bool
    gaps: list[datetime]
    blocking_gaps: list[datetime]
    expected_gaps: list[datetime]
    errors: list[str]
    warnings: list[str]
    last_timestamp: datetime | None = None
    age_seconds: float | None = None


def _crosses_weekend(previous: datetime, current: datetime) -> bool:
    cursor = previous.date()
    while cursor <= current.date():
        if cursor.weekday() >= 5:
            return True
        cursor += timedelta(days=1)
    return False


def _is_expected_market_closure(previous: datetime, current: datetime, timeframe: Timeframe) -> bool:
    """Classify normal XAU/USD maintenance, weekend and holiday closures."""
    delta = (current - previous).total_seconds()
    seconds = TIMEFRAME_SECONDS[timeframe]
    if timeframe == Timeframe.mn1:
        year = previous.year + (1 if previous.month == 12 else 0)
        month = 1 if previous.month == 12 else previous.month + 1
        expected = previous.replace(year=year, month=month, day=1)
        return abs((current - expected).total_seconds()) <= 36 * 3600
    if timeframe == Timeframe.w1:
        return delta <= 15 * 86400
    if timeframe == Timeframe.d1:
        return delta <= 4 * 86400
    # Gold commonly has a short weekday maintenance break.
    if delta <= max(seconds * 2.2, 3 * 3600):
        return True
    # Weekend close can vary by broker and daylight-saving schedule.
    return delta <= 4 * 86400 and _crosses_weekend(previous, current)


def candles_frame(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame([c.model_dump() for c in candles]).set_index("timestamp")


def validate_candles(candles: list[Candle], timeframe: Timeframe, now: datetime | None = None) -> DataQuality:
    timeframe = Timeframe(timeframe)
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    gaps: list[datetime] = []
    blocking_gaps: list[datetime] = []
    expected_gaps: list[datetime] = []
    warnings: list[str] = []
    seconds = TIMEFRAME_SECONDS[timeframe]
    if len(candles) < 2:
        return DataQuality(False, True, [], [], [], ["Insufficient candles"], [], candles[-1].timestamp if candles else None, None)
    for index, (previous, current) in enumerate(zip(candles, candles[1:]), start=1):
        delta = (current.timestamp - previous.timestamp).total_seconds()
        if delta <= 0:
            gaps.append(current.timestamp)
            blocking_gaps.append(current.timestamp)
        elif delta > seconds * 2.2:
            if _is_expected_market_closure(previous.timestamp, current.timestamp, timeframe):
                expected_gaps.append(current.timestamp)
            else:
                gaps.append(current.timestamp)
                # Old broker-history gaps remain visible but do not make a live feed unusable.
                if index >= len(candles) - 50:
                    blocking_gaps.append(current.timestamp)
    ordered = sorted(candles, key=lambda c: c.timestamp)
    for previous, current in zip(ordered, ordered[1:]):
        delta = (current.timestamp - previous.timestamp).total_seconds()
        if delta > seconds * 2.2 and current.timestamp not in gaps and current.timestamp not in expected_gaps:
            gaps.append(current.timestamp)
    if gaps and not blocking_gaps:
        warnings.append(f"{len(gaps)} historical data gap(s) outside the live validation window")
    if expected_gaps:
        warnings.append(f"{len(expected_gaps)} expected market-closure gap(s) ignored")
    if any(c.high < c.low or min(c.open, c.close) < c.low or max(c.open, c.close) > c.high for c in ordered):
        errors.append("Invalid OHLC candle")
    age = max(0.0, (now - ordered[-1].timestamp).total_seconds())
    stale = age > seconds * 2.5
    if stale:
        errors.append("Market data is stale")
    if blocking_gaps:
        errors.append(f"{len(blocking_gaps)} recent unexpected candle gap(s)")
    return DataQuality(not errors, stale, gaps, blocking_gaps, expected_gaps, errors, warnings, ordered[-1].timestamp, round(age, 3))


def indicators(candles: list[Candle]) -> dict:
    frame = candles_frame(candles)
    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    previous = close.shift(1)
    true_range = pd.concat([(high - low), (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    recent = atr.iloc[-1]
    baseline = atr.tail(100).median()
    regime = "HIGH" if recent > baseline * 1.35 else "LOW" if recent < baseline * 0.7 else "NORMAL"
    return {"ema20": ema20.tolist(), "ema50": ema50.tolist(), "rsi": rsi.fillna(50).tolist(), "atr": atr.fillna(0).tolist(), "latest": {"ema20": float(ema20.iloc[-1]), "ema50": float(ema50.iloc[-1]), "rsi": float(rsi.fillna(50).iloc[-1]), "atr": float(recent), "volatility_regime": regime}}

