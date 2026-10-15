from __future__ import annotations

from .domain import Candle, Side, Timeframe, Zone
from .market import indicators


def _swings(candles: list[Candle], width: int = 3) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    for index in range(width, len(candles) - width):
        window = candles[index - width:index + width + 1]
        if candles[index].high == max(c.high for c in window):
            highs.append(index)
        if candles[index].low == min(c.low for c in window):
            lows.append(index)
    return highs, lows


def detect_zones(candles: list[Candle], timeframe: Timeframe, limit: int = 8) -> list[Zone]:
    """Detect reaction zones using confirmed swings; the incomplete last bar is never a pivot."""
    timeframe = Timeframe(timeframe)
    complete = [c for c in candles if c.complete]
    if len(complete) < 30:
        return []
    metrics = indicators(complete)
    atr = max(metrics["latest"]["atr"], 0.01)
    # Incomplete candles may be displayed for live context, but cannot confirm a
    # structural break or reorder persistent zones until they close.
    current = complete[-1].close
    highs, lows = _swings(complete)
    zones: list[Zone] = []
    for direction, indexes in ((Side.buy, lows), (Side.sell, highs)):
        for index in indexes[-12:]:
            pivot = complete[index]
            body_low = min(pivot.open, pivot.close)
            body_high = max(pivot.open, pivot.close)
            if direction == Side.buy:
                lower, upper = pivot.low, body_low + atr * 0.18
                invalidation = lower - atr * 0.35
                entry = upper
                stop = invalidation
                target = entry + max(atr * 2.2, (entry - stop) * 1.5)
                broken = current < invalidation
                rejection = pivot.close > pivot.open
            else:
                lower, upper = body_high - atr * 0.18, pivot.high
                invalidation = upper + atr * 0.35
                entry = lower
                stop = invalidation
                target = entry - max(atr * 2.2, (stop - entry) * 1.5)
                broken = current > invalidation
                rejection = pivot.close < pivot.open
            touches = sum(1 for candle in complete[index + 1:] if candle.low <= upper and candle.high >= lower)
            displacement = abs(complete[min(index + 3, len(complete) - 1)].close - pivot.close) / atr
            freshness = max(0.15, 1 - (len(complete) - index) / max(len(complete), 1))
            score = max(0, min(100, 38 + min(displacement, 3) * 12 + (10 if rejection else 0) + freshness * 20 - min(touches, 4) * 6 - (60 if broken else 0)))
            risk = abs(entry - stop)
            rr = abs(target - entry) / risk if risk else 0
            evidence = ["confirmed swing low" if direction == Side.buy else "confirmed swing high", f"{displacement:.1f} ATR displacement", "rejection candle" if rejection else "structural pivot", f"{touches} subsequent touch(es)"]
            zone_id = f"{timeframe.value}:{direction.value}:{int(pivot.timestamp.timestamp())}:{round(pivot.high if direction == Side.sell else pivot.low, 2)}"
            zones.append(Zone(id=zone_id, direction=direction, timeframe=timeframe, lower=round(lower, 2), upper=round(upper, 2), invalidation=round(invalidation, 2), entry=round(entry, 2), stop_loss=round(stop, 2), take_profit=round(target, 2), reward_risk=round(rr, 2), quality_score=round(score, 1), touches=touches, broken=broken, relevance=round(freshness, 2), evidence=evidence, explanation=f"{direction.value} reaction area from a confirmed {timeframe.value} pivot with measurable displacement; it is {'invalidated' if broken else 'active'} and is not assumed to hold."))
    active = [zone for zone in zones if not zone.broken]
    return sorted(active, key=lambda zone: (-zone.quality_score, abs(((zone.lower + zone.upper) / 2) - current)))[:limit]


def structure_summary(candles: list[Candle]) -> dict:
    complete = [c for c in candles if c.complete]
    highs, lows = _swings(complete)
    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "NEUTRAL", "bos": None, "choch": None, "liquidity_sweep": False}
    higher_high = complete[highs[-1]].high > complete[highs[-2]].high
    higher_low = complete[lows[-1]].low > complete[lows[-2]].low
    trend = "BULLISH" if higher_high and higher_low else "BEARISH" if not higher_high and not higher_low else "RANGING"
    last = complete[-1]
    bos = "BULLISH" if last.close > complete[highs[-1]].high else "BEARISH" if last.close < complete[lows[-1]].low else None
    choch = bos if (trend == "BULLISH" and bos == "BEARISH") or (trend == "BEARISH" and bos == "BULLISH") else None
    sweep = last.high > complete[highs[-1]].high and last.close < complete[highs[-1]].high or last.low < complete[lows[-1]].low and last.close > complete[lows[-1]].low
    return {"trend": trend, "bos": bos, "choch": choch, "liquidity_sweep": sweep, "previous_high": complete[highs[-1]].high, "previous_low": complete[lows[-1]].low}

