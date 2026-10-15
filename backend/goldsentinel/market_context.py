from __future__ import annotations

from .candle_analysis import CandleAnalysisEngine
from .domain import Candle, MarketContext, Timeframe


CONTEXT_FRAMES = (Timeframe.h4, Timeframe.h1, Timeframe.m15, Timeframe.m5, Timeframe.m1)
WEIGHTS = {Timeframe.h4: 3.0, Timeframe.h1: 3.0, Timeframe.m15: 2.2, Timeframe.m5: 1.5, Timeframe.m1: 0.35}


class MarketContextEngine:
    def __init__(self, analyzer: CandleAnalysisEngine | None = None) -> None:
        self.analyzer = analyzer or CandleAnalysisEngine()

    def build(self, symbol: str, candles_by_timeframe: dict[Timeframe, list[Candle]]) -> MarketContext:
        analyses = {
            timeframe.value: self.analyzer.analyze(candles_by_timeframe[timeframe], timeframe)
            for timeframe in CONTEXT_FRAMES
        }
        direction = 0.0
        total = 0.0
        for timeframe in CONTEXT_FRAMES:
            analysis = analyses[timeframe.value]
            weight = WEIGHTS[timeframe] * max(0.35, analysis.trend_strength / 100)
            total += weight
            direction += weight if analysis.bias == "BULLISH" else -weight if analysis.bias == "BEARISH" else 0
        normalized = direction / max(total, 1e-9)
        bias = "BULLISH" if normalized >= 0.22 else "BEARISH" if normalized <= -0.22 else "NEUTRAL"
        h1 = analyses[Timeframe.h1.value]
        m15 = analyses[Timeframe.m15.value]
        if h1.regime.startswith("STRONG") and m15.bias == h1.bias:
            regime = h1.regime
        elif "BREAKOUT" in m15.regime:
            regime = m15.regime
        elif bias == "NEUTRAL":
            regime = "RANGE_OR_CHOP"
        elif h1.bias != m15.bias:
            regime = "POSSIBLE_REVERSAL"
        else:
            regime = f"{bias}_TREND"
        reasons = [
            f"H4 {analyses['H4'].bias.lower()} / H1 {h1.bias.lower()}",
            f"M15 {m15.bias.lower()} / M5 {analyses['M5'].bias.lower()}",
            f"Weighted directional context {normalized:+.2f}",
        ]
        return MarketContext(
            symbol=symbol,
            timestamp=max(item.timestamp for item in analyses.values()),
            regime=regime,
            directional_bias=bias,
            analyses=analyses,
            reasons=reasons,
        )
