from datetime import datetime, timedelta, timezone

from goldsentinel.domain import Candle
from goldsentinel.market import indicators, validate_candles
from goldsentinel.zones import detect_zones


def test_all_timeframes_are_retrievable(broker, timeframe):
    rows = broker.candles("XAUUSD.m", timeframe, 100)
    assert len(rows) == 100
    assert rows[-1].complete is False
    quality = validate_candles(rows, timeframe)
    assert quality.valid, quality.errors
    assert quality.stale is False


def test_indicators_do_not_mutate_candles(candles):
    before = candles[-1].model_copy()
    result = indicators(candles)
    assert result["latest"]["atr"] > 0
    assert candles[-1] == before


def test_gap_detection(candles):
    candles[100] = candles[100].model_copy(update={"timestamp": candles[99].timestamp + timedelta(minutes=5)})
    result = validate_candles(candles, timeframe="M1", now=candles[-1].timestamp)
    assert result.gaps
    assert not result.valid


def test_zone_detector_ignores_incomplete_last_bar(candles):
    first = detect_zones(candles, timeframe="M1")
    candles[-1] = Candle(timestamp=candles[-1].timestamp, open=1000, high=5000, low=900, close=4000, complete=False)
    second = detect_zones(candles, timeframe="M1")
    normalize = lambda zones: [zone.model_dump(exclude={"id", "created_at"}) for zone in zones]
    assert normalize(first) == normalize(second)

