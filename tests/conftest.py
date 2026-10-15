from datetime import datetime, timedelta, timezone

import pytest

from goldsentinel.brokers.mock import MockBroker
from goldsentinel.domain import Candle, Timeframe


@pytest.fixture
def broker():
    value = MockBroker()
    value.connect()
    return value


@pytest.fixture
def candles():
    start = datetime.now(timezone.utc) - timedelta(minutes=199)
    result = []
    price = 2600.0
    for i in range(200):
        wave = ((i % 20) - 10) * 0.25
        close = price + wave * 0.15 + 0.05
        result.append(Candle(timestamp=start + timedelta(minutes=i), open=price, high=max(price, close) + 0.5, low=min(price, close) - 0.5, close=close, volume=100 + i, complete=i < 199))
        price = close
    return result


@pytest.fixture(params=list(Timeframe))
def timeframe(request):
    return request.param

