from __future__ import annotations

from datetime import timedelta
from hashlib import sha256

from .domain import MarketContext, Quote, SetupType, Side, StrategyProfile, Timeframe, TradeSetup, Zone


PROFILE_THRESHOLDS = {
    StrategyProfile.conservative: {
        SetupType.zone_reversal: 72,
        SetupType.trend_continuation: 76,
        SetupType.pullback: 72,
        SetupType.breakout_retest: 78,
    },
    StrategyProfile.balanced: {
        SetupType.zone_reversal: 58,
        SetupType.trend_continuation: 58,
        SetupType.pullback: 60,
        SetupType.breakout_retest: 62,
    },
    StrategyProfile.opportunistic: {
        SetupType.zone_reversal: 48,
        SetupType.trend_continuation: 48,
        SetupType.pullback: 50,
        SetupType.breakout_retest: 52,
    },
}


class SetupEngine:
    def detect(self, context: MarketContext, quote: Quote, zones: list[Zone], profile: StrategyProfile) -> list[TradeSetup]:
        m15 = context.analyses["M15"]
        m5 = context.analyses["M5"]
        h1 = context.analyses["H1"]
        price = (quote.bid + quote.ask) / 2
        atr = max(m15.atr, m5.atr, quote.spread * 3, 0.01)
        proposals: list[tuple[SetupType, Side, float, bool, str, list[str]]] = []

        for zone in [item for item in zones if not item.broken]:
            distance = max(0.0, price - zone.upper, zone.lower - price)
            side_bias = "BULLISH" if zone.direction == Side.buy else "BEARISH"
            trigger = any(side_bias in pattern for pattern in m5.patterns)
            if distance <= atr * 0.45 and (trigger or m5.bias == side_bias):
                score = zone.quality_score * 0.65 + m5.evidence_score * 0.35 + (7 if trigger else 0)
                proposals.append((SetupType.zone_reversal, zone.direction, score, True, f"M5 reaction at {zone.direction.value} zone", [*zone.evidence, *m5.reasons]))

        if h1.bias in {"BULLISH", "BEARISH"} and m15.bias == h1.bias and m5.bias == h1.bias:
            side = Side.buy if h1.bias == "BULLISH" else Side.sell
            score = h1.trend_strength * 0.35 + m15.evidence_score * 0.4 + m5.evidence_score * 0.25
            proposals.append((SetupType.trend_continuation, side, score, False, f"M5 {side.value} momentum aligned with H1/M15 trend", [*context.reasons, *m5.reasons]))

        trend_side = Side.buy if h1.bias == "BULLISH" else Side.sell if h1.bias == "BEARISH" else None
        if trend_side and m15.bias == h1.bias:
            pulled_back = abs(price - m15.ema20) <= atr * 0.8
            resumed = m5.momentum == h1.bias or any(h1.bias in item and "MOMENTUM" in item for item in m5.patterns)
            if pulled_back and resumed:
                score = m15.evidence_score * 0.45 + m5.evidence_score * 0.4 + h1.trend_strength * 0.15 + 5
                proposals.append((SetupType.pullback, trend_side, score, False, f"M15 EMA pullback with M5 {trend_side.value} resumption", [*m15.reasons, *m5.reasons]))

        breakout = m15.bos or m5.bos
        if breakout and m5.bias == breakout:
            side = Side.buy if breakout == "BULLISH" else Side.sell
            score = m15.evidence_score * 0.55 + m5.evidence_score * 0.45 + (7 if "INSIDE_BAR" in m5.patterns else 0)
            proposals.append((SetupType.breakout_retest, side, score, False, f"{breakout.title()} structure break confirmed by M5", [*m15.reasons, *m5.reasons]))

        result: list[TradeSetup] = []
        for setup_type, side, score, in_zone, trigger, reasons in proposals:
            if score < PROFILE_THRESHOLDS[profile][setup_type]:
                continue
            entry = quote.ask if side == Side.buy else quote.bid
            invalidation = m5.swing_low if side == Side.buy else m5.swing_high
            fallback = entry - atr * 1.15 if side == Side.buy else entry + atr * 1.15
            stop = min(invalidation, fallback) if side == Side.buy and invalidation else max(invalidation, fallback) if invalidation else fallback
            risk = abs(entry - stop)
            if risk <= quote.spread * 1.5:
                continue
            opposing = [z for z in zones if not z.broken and z.direction != side and ((z.lower > entry) if side == Side.buy else (z.upper < entry))]
            nearest = min(opposing, key=lambda z: abs(z.entry - entry), default=None)
            desired = entry + risk * 1.8 if side == Side.buy else entry - risk * 1.8
            if nearest:
                boundary = nearest.lower if side == Side.buy else nearest.upper
                target = min(desired, boundary - quote.spread) if side == Side.buy else max(desired, boundary + quote.spread)
            else:
                target = desired
            rr = abs(target - entry) / risk
            minimum = 1.55 if profile == StrategyProfile.conservative else 1.35 if profile == StrategyProfile.balanced else 1.15
            if rr < minimum:
                continue
            raw = f"{context.symbol}|{setup_type.value}|{side.value}|{m5.timestamp.isoformat()}|{round(stop, 2)}"
            result.append(TradeSetup(
                fingerprint=sha256(raw.encode()).hexdigest()[:24],
                setup_type=setup_type,
                side=side,
                timeframe=Timeframe.m5,
                trigger_timestamp=m5.timestamp,
                trigger=trigger,
                stop_loss=round(stop, 5),
                take_profit=round(target, 5),
                evidence_score=round(min(100, score), 2),
                in_zone=in_zone,
                reasons=reasons,
                expires_at=m5.timestamp + timedelta(minutes=20),
            ))
        return sorted(result, key=lambda item: item.evidence_score, reverse=True)
