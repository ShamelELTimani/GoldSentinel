from datetime import datetime, timedelta, timezone

from goldsentinel.domain import (
    AccountSnapshot,
    Candle,
    DecisionAction,
    Quote,
    RiskSettings,
    Side,
    StrategyProfile,
    Timeframe,
    Zone,
)
from goldsentinel.risk import RiskManager
from goldsentinel.strategy import StrategyEngine


def _zone(zone_id: str, lower: float, upper: float, stop: float, target: float) -> Zone:
    return Zone(
        id=zone_id,
        direction=Side.buy,
        timeframe=Timeframe.m15,
        lower=lower,
        upper=upper,
        invalidation=stop,
        entry=(lower + upper) / 2,
        stop_loss=stop,
        take_profit=target,
        reward_risk=2,
        quality_score=82,
        touches=1,
        relevance=1,
        evidence=["fresh reaction"],
        explanation="test zone",
    )


def test_strategy_evaluates_beyond_nearest_rejected_zone(monkeypatch):
    now = datetime.now(timezone.utc)
    candles = [
        Candle(
            timestamp=now - timedelta(minutes=15 * (40 - index)),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=100,
            complete=index < 39,
        )
        for index in range(40)
    ]
    monkeypatch.setattr(
        "goldsentinel.strategy.indicators",
        lambda _candles: {
            "latest": {"atr": 2, "rsi": 50, "volatility_regime": "NORMAL"}
        },
    )
    monkeypatch.setattr(
        "goldsentinel.strategy.structure_summary",
        lambda _candles: {
            "trend": "RANGING",
            "previous_low": 99.5,
            "previous_high": 101,
            "liquidity_sweep": False,
        },
    )
    nearest_bad_rr = _zone("nearest", 99.9, 100.1, 99, 100.5)
    qualifying = _zone("qualified", 99.4, 99.7, 97, 105.5)
    candidate = StrategyEngine(RiskManager()).evaluate(
        "XAUUSD.m",
        Timeframe.m15,
        candles,
        [nearest_bad_rr, qualifying],
        Quote(symbol="XAUUSD.m", bid=100, ask=100.2, timestamp=now, source="MOCK"),
        AccountSnapshot(
            balance=100_000,
            equity=100_000,
            free_margin=100_000,
            margin=0,
            leverage=500,
        ),
        [],
        RiskSettings(preferred_min_rr=1.5),
        StrategyProfile.balanced,
        {
            "point": 0.01,
            "trade_stops_level": 10,
            "contract_size": 100,
            "volume_min": 0.01,
            "volume_max": 100,
            "volume_step": 0.01,
        },
        {"status": "NORMAL", "factor": 1, "block": False, "reason": "clear"},
    )

    assert candidate.action == DecisionAction.buy
    assert candidate.zone_id == "qualified"
    assert candidate.entry == 100.2
    assert candidate.evaluated_zones == 2
    assert any("2 active zones" in reason for reason in candidate.rationale)
