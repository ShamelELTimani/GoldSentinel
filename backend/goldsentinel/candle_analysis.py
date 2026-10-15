from __future__ import annotations

from statistics import mean

from .domain import Candle, CandleAnalysis, Timeframe
from .market import indicators


def _confirmed_swings(candles: list[Candle], radius: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for index in range(radius, len(candles) - radius):
        window = candles[index - radius : index + radius + 1]
        if candles[index].high == max(item.high for item in window):
            highs.append((index, candles[index].high))
        if candles[index].low == min(item.low for item in window):
            lows.append((index, candles[index].low))
    return highs, lows


class CandleAnalysisEngine:
    """Deterministic, completed-candle analysis with no future-bar dependency."""

    def analyze(self, candles: list[Candle], timeframe: Timeframe) -> CandleAnalysis:
        completed = [item for item in candles if item.complete]
        if len(completed) < 55:
            raise ValueError(f"{timeframe.value} needs at least 55 completed candles")
        rows = completed[-240:]
        metric = indicators(rows)
        latest = rows[-1]
        previous = rows[-2]
        atr = max(float(metric["latest"]["atr"]), 1e-9)
        ema20 = float(metric["latest"]["ema20"])
        ema50 = float(metric["latest"]["ema50"])
        rsi = float(metric["latest"]["rsi"])
        body = abs(latest.close - latest.open)
        candle_range = max(latest.high - latest.low, 1e-9)
        upper_wick = latest.high - max(latest.open, latest.close)
        lower_wick = min(latest.open, latest.close) - latest.low
        average_body = mean(abs(item.close - item.open) for item in rows[-21:-1])
        average_range = mean(item.high - item.low for item in rows[-21:-1])
        average_volume = mean(item.volume for item in rows[-21:-1]) or 1
        patterns: list[str] = []
        bullish = 0.0
        bearish = 0.0

        if latest.close > latest.open and previous.close < previous.open and latest.open <= previous.close and latest.close >= previous.open:
            patterns.append("BULLISH_ENGULFING")
            bullish += 16
        if latest.close < latest.open and previous.close > previous.open and latest.open >= previous.close and latest.close <= previous.open:
            patterns.append("BEARISH_ENGULFING")
            bearish += 16
        if lower_wick >= body * 2.2 and latest.close >= latest.low + candle_range * 0.62:
            patterns.append("BULLISH_PIN_BAR")
            bullish += 13
        if upper_wick >= body * 2.2 and latest.close <= latest.low + candle_range * 0.38:
            patterns.append("BEARISH_PIN_BAR")
            bearish += 13
        if body >= average_body * 1.55 and body / candle_range >= 0.65:
            direction = "BULLISH" if latest.close > latest.open else "BEARISH"
            patterns.append(f"{direction}_MOMENTUM_CANDLE")
            bullish += 14 if direction == "BULLISH" else 0
            bearish += 14 if direction == "BEARISH" else 0
        if latest.high < previous.high and latest.low > previous.low:
            patterns.append("INSIDE_BAR")
        recent = rows[-4:]
        if all(item.close > item.open for item in recent[-3:]):
            patterns.append("CONSECUTIVE_BULLISH")
            bullish += 9
        if all(item.close < item.open for item in recent[-3:]):
            patterns.append("CONSECUTIVE_BEARISH")
            bearish += 9
        if candle_range >= average_range * 1.6:
            patterns.append("VOLATILITY_EXPANSION")
        elif candle_range <= average_range * 0.65:
            patterns.append("VOLATILITY_CONTRACTION")
        if latest.volume >= average_volume * 1.45:
            patterns.append("VOLUME_EXPANSION")

        highs, lows = _confirmed_swings(rows)
        swing_high = highs[-1][1] if highs else max(item.high for item in rows[-20:-1])
        swing_low = lows[-1][1] if lows else min(item.low for item in rows[-20:-1])
        prior_high = highs[-2][1] if len(highs) > 1 else swing_high
        prior_low = lows[-2][1] if len(lows) > 1 else swing_low
        higher_structure = len(highs) > 1 and len(lows) > 1 and swing_high > prior_high and swing_low > prior_low
        lower_structure = len(highs) > 1 and len(lows) > 1 and swing_high < prior_high and swing_low < prior_low
        bos = None
        if latest.close > swing_high:
            bos = "BULLISH"
            patterns.append("BULLISH_BOS")
            bullish += 18
        elif latest.close < swing_low:
            bos = "BEARISH"
            patterns.append("BEARISH_BOS")
            bearish += 18
        liquidity_sweep = False
        if latest.high > swing_high and latest.close < swing_high:
            patterns.extend(["BEARISH_LIQUIDITY_SWEEP", "FAILED_UPSIDE_BREAKOUT"])
            bearish += 15
            liquidity_sweep = True
        if latest.low < swing_low and latest.close > swing_low:
            patterns.extend(["BULLISH_LIQUIDITY_SWEEP", "FAILED_DOWNSIDE_BREAKOUT"])
            bullish += 15
            liquidity_sweep = True

        ema_gap = (ema20 - ema50) / atr
        if ema_gap > 0.18:
            bullish += min(20, 8 + ema_gap * 5)
        elif ema_gap < -0.18:
            bearish += min(20, 8 + abs(ema_gap) * 5)
        bullish += 12 if higher_structure else 0
        bearish += 12 if lower_structure else 0
        bullish += 5 if rsi >= 52 else 0
        bearish += 5 if rsi <= 48 else 0

        if max(bullish, bearish) < 20 or abs(bullish - bearish) < 8:
            bias = "NEUTRAL"
        else:
            bias = "BULLISH" if bullish > bearish else "BEARISH"
        trend_strength = min(100, abs(ema_gap) * 22 + (18 if higher_structure or lower_structure else 0) + abs(rsi - 50) * 1.2)
        if trend_strength >= 55 and bias != "NEUTRAL":
            regime = f"STRONG_{bias}_TREND"
        elif average_range and mean(item.high - item.low for item in rows[-10:]) <= average_range * 0.75:
            regime = "CONSOLIDATION"
        elif bos:
            regime = f"{bos}_BREAKOUT"
        elif bias == "NEUTRAL":
            regime = "RANGE_OR_CHOP"
        else:
            regime = f"{bias}_TREND"
        choch = None
        if bos == "BEARISH" and higher_structure:
            choch = "BEARISH"
        elif bos == "BULLISH" and lower_structure:
            choch = "BULLISH"
        if choch:
            patterns.append(f"{choch}_CHOCH")
        momentum = "BULLISH" if latest.close > latest.open and latest.close > ema20 else "BEARISH" if latest.close < latest.open and latest.close < ema20 else "MIXED"
        volatility = "EXPANDING" if "VOLATILITY_EXPANSION" in patterns else "CONTRACTING" if "VOLATILITY_CONTRACTION" in patterns else metric["latest"]["volatility_regime"]
        evidence_score = min(100, max(bullish, bearish) + trend_strength * 0.25)
        reasons = [
            f"{timeframe.value} regime {regime}",
            f"EMA20/EMA50 ATR gap {ema_gap:.2f}",
            f"RSI {rsi:.1f}; momentum {momentum.lower()}",
        ]
        if patterns:
            reasons.append("Patterns: " + ", ".join(patterns[-6:]))
        invalidation = swing_low if bias == "BULLISH" else swing_high if bias == "BEARISH" else None
        return CandleAnalysis(
            timeframe=timeframe,
            timestamp=latest.timestamp,
            bias=bias,
            regime=regime,
            trend_strength=round(trend_strength, 2),
            momentum=momentum,
            volatility=volatility,
            patterns=list(dict.fromkeys(patterns)),
            reasons=reasons,
            evidence_score=round(evidence_score, 2),
            invalidation=invalidation,
            ema20=ema20,
            ema50=ema50,
            rsi=rsi,
            atr=atr,
            swing_high=swing_high,
            swing_low=swing_low,
            bos=bos,
            choch=choch,
            liquidity_sweep=liquidity_sweep,
        )
