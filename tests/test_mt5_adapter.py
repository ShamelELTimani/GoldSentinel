import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from goldsentinel.brokers.mock import MockBroker
from goldsentinel.config import Settings
from goldsentinel.domain import Quote, RuntimeSettings, Timeframe, TradingMode
from goldsentinel.market import validate_candles
from goldsentinel.service import GoldSentinelService


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_TRADE_EXECUTION_MARKET = 2

    def __init__(self):
        for index, timeframe in enumerate(Timeframe, start=1):
            setattr(self, f"TIMEFRAME_{timeframe.value}", index)
        self.requested_timeframes = []

    def initialize(self):
        return True

    def shutdown(self):
        return None

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def last_error(self):
        return (0, "ok")

    def symbol_info_tick(self, symbol):
        broker_wall_time = datetime.now(timezone.utc) + timedelta(hours=3)
        return SimpleNamespace(bid=2700.0, ask=2700.2, time_msc=int(broker_wall_time.timestamp() * 1000))

    def symbol_info(self, symbol):
        return SimpleNamespace(filling_mode=1, trade_exemode=2, volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_contract_size=100.0, trade_stops_level=10, point=0.01)

    def copy_rates_from_pos(self, symbol, timeframe, offset, count):
        self.requested_timeframes.append(timeframe)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        rows = []
        for index in range(count):
            timestamp = now - timedelta(minutes=count - index - 1) + timedelta(hours=3)
            rows.append({"time": int(timestamp.timestamp()), "open": 2700 + index, "high": 2701 + index, "low": 2699 + index, "close": 2700.5 + index, "tick_volume": 100, "spread": 20})
        return rows


def test_mt5_adapter_normalizes_broker_clock_and_marks_forming_bar(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from goldsentinel.brokers.mt5 import MT5Broker

    broker = MT5Broker()
    assert broker.connect()
    quote = broker.quote("XAUUSD.m")
    assert abs((datetime.now(timezone.utc) - quote.timestamp).total_seconds()) < 10
    candles = broker.candles("XAUUSD.m", Timeframe.m1, 5)
    assert all(candle.complete for candle in candles[:-1])
    assert candles[-1].complete is False
    assert abs((datetime.now(timezone.utc) - candles[-1].timestamp).total_seconds()) < 90


def test_mt5_adapter_maps_all_nine_timeframes(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from goldsentinel.brokers.mt5 import MT5Broker

    broker = MT5Broker()
    broker.connect()
    for timeframe in Timeframe:
        assert len(broker.candles("XAUUSD.m", timeframe, 3)) == 3
    assert len(set(fake.requested_timeframes)) == 9


def test_mt5_filling_mode_uses_broker_flags_without_missing_constants(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from goldsentinel.brokers.mt5 import MT5Broker

    broker = MT5Broker()
    broker.connect()
    spec = broker.symbol_spec("XAUUSD.m")
    assert spec["filling_modes"] == ["FOK"]


def test_validator_ignores_expected_weekend_closure(candles):
    candles[100] = candles[100].model_copy(update={"timestamp": candles[99].timestamp + timedelta(days=2)})
    for index in range(101, len(candles)):
        candles[index] = candles[index].model_copy(update={"timestamp": candles[index].timestamp + timedelta(days=2)})
    quality = validate_candles(candles, Timeframe.m1, now=candles[-1].timestamp)
    assert quality.valid
    assert quality.expected_gaps
    assert not quality.blocking_gaps


class MockedMT5Broker(MockBroker):
    def quote(self, symbol: str) -> Quote:
        quote = super().quote(symbol)
        return quote.model_copy(update={"source": "MT5"})


class MemoryRepository:
    def __init__(self):
        self.settings = RuntimeSettings(mode=TradingMode.full_auto, paused=True)
        self.events = []

    def runtime_settings(self):
        return self.settings.model_copy(update={"armed": False})

    def save_runtime_settings(self, value):
        self.settings = value.model_copy(update={"armed": False})

    def audit(self, category, message, payload=None, level="INFO"):
        self.events.append((category, message, level))


def test_readiness_exposes_pause_disarm_and_news_block_without_orders():
    broker = MockedMT5Broker()
    repository = MemoryRepository()
    service = GoldSentinelService(Settings(_env_file=None, broker="mt5", calendar_url=None), broker=broker, repository=repository)
    service.start()
    service.market_snapshot(Timeframe.m15)
    status = service.status()
    failed = {item["id"] for item in status["readiness"]["checks"] if not item["ready"]}
    assert {"news", "pause", "armed"}.issubset(failed)
    assert status["readiness"]["can_arm"] is False
    service.set_paused(False)
    assert service.runtime.paused is False
    assert service.runtime.armed is False
    try:
        service.set_armed(True)
    except ValueError as exc:
        assert "calendar" in str(exc).lower()
    else:
        raise AssertionError("Unverified news provider must prevent arming under conservative policy")
    assert broker.positions() == []


def test_mode_and_settings_changes_force_disarm():
    broker = MockedMT5Broker()
    repository = MemoryRepository()
    service = GoldSentinelService(Settings(_env_file=None, broker="mt5"), broker=broker, repository=repository)
    broker.connect()
    service.runtime.armed = True
    updated = service.runtime.model_copy(update={"mode": TradingMode.confirmation, "armed": True})
    result = service.update_runtime(updated)
    assert result.mode == TradingMode.confirmation
    assert result.armed is False

