from __future__ import annotations

from datetime import datetime, timedelta, timezone

from goldsentinel.brokers.mock import MockBroker
from goldsentinel.candle_analysis import CandleAnalysisEngine
from goldsentinel.domain import (
    BrokerResult,
    Candle,
    CandleAnalysis,
    DecisionAction,
    MarketContext,
    Position,
    RuntimeSettings,
    SetupType,
    Side,
    StrategyProfile,
    Timeframe,
    TradeCandidate,
    TradingMode,
    Zone,
    utcnow,
)
from goldsentinel.execution import ExecutionManager
from goldsentinel.historical_data import HistoricalDataPipeline
from goldsentinel.position_manager import PositionManager
from goldsentinel.setups import SetupEngine


def _analysis(frame: Timeframe, bias: str = "BULLISH", *, bos: str | None = None) -> CandleAnalysis:
    return CandleAnalysis(
        timeframe=frame,
        timestamp=utcnow() - timedelta(minutes=5),
        bias=bias,
        regime=f"{bias}_TREND" if bias != "NEUTRAL" else "RANGE_OR_CHOP",
        trend_strength=90 if bias != "NEUTRAL" else 10,
        momentum=bias if bias != "NEUTRAL" else "MIXED",
        volatility="NORMAL",
        patterns=[f"{bias}_MOMENTUM_CANDLE"] if bias != "NEUTRAL" else [],
        reasons=[f"{frame.value} test evidence"],
        evidence_score=90 if bias != "NEUTRAL" else 10,
        invalidation=98 if bias == "BULLISH" else 102 if bias == "BEARISH" else None,
        ema20=100,
        ema50=99 if bias == "BULLISH" else 101,
        rsi=60 if bias == "BULLISH" else 40,
        atr=1,
        swing_high=102,
        swing_low=98,
        bos=bos,
    )


def _context(bias: str = "BULLISH", *, bos: str | None = None) -> MarketContext:
    analyses = {frame.value: _analysis(frame, bias, bos=bos if frame in {Timeframe.m5, Timeframe.m15} else None) for frame in (Timeframe.h4, Timeframe.h1, Timeframe.m15, Timeframe.m5, Timeframe.m1)}
    return MarketContext(symbol="XAUUSD.m", timestamp=utcnow(), regime=f"{bias}_TREND", directional_bias=bias, analyses=analyses, reasons=["test context"])


def test_candle_analysis_ignores_forming_candle(broker):
    rows = broker.candles("XAUUSD.m", Timeframe.m5, 90)
    analyzer = CandleAnalysisEngine()
    baseline = analyzer.analyze(rows, Timeframe.m5)
    forming = rows[-1].model_copy(update={"open": 1000, "high": 5000, "low": 900, "close": 4900, "complete": False})
    changed = analyzer.analyze([*rows[:-1], forming], Timeframe.m5)
    assert changed.model_dump() == baseline.model_dump()


def test_between_zone_continuation_is_a_first_class_setup():
    from goldsentinel.domain import Quote

    setups = SetupEngine().detect(
        _context("BULLISH"),
        Quote(symbol="XAUUSD.m", bid=100, ask=100.2, timestamp=utcnow(), source="MOCK"),
        [],
        StrategyProfile.balanced,
    )
    continuation = next(item for item in setups if item.setup_type == SetupType.trend_continuation)
    assert continuation.side == Side.buy
    assert continuation.in_zone is False


def test_setup_engine_detects_zone_reversal_pullback_and_breakout():
    from goldsentinel.domain import Quote

    quote = Quote(symbol="XAUUSD.m", bid=100, ask=100.2, timestamp=utcnow(), source="MOCK")
    zone = Zone(
        direction=Side.buy, timeframe=Timeframe.m15, lower=99.5, upper=100.1,
        invalidation=98, entry=100, stop_loss=98, take_profit=104,
        reward_risk=2, quality_score=85, relevance=.9, evidence=["support rejection"],
        explanation="test support",
    )
    setups = SetupEngine().detect(_context("BULLISH", bos="BULLISH"), quote, [zone], StrategyProfile.balanced)
    kinds = {item.setup_type for item in setups}
    assert {SetupType.zone_reversal, SetupType.pullback, SetupType.breakout_retest}.issubset(kinds)


def test_weak_conflicting_context_produces_no_setup():
    from goldsentinel.domain import Quote

    context = _context("NEUTRAL")
    context.analyses["H1"] = _analysis(Timeframe.h1, "BULLISH")
    context.analyses["M15"] = _analysis(Timeframe.m15, "BEARISH")
    quote = Quote(symbol="XAUUSD.m", bid=100, ask=100.2, timestamp=utcnow(), source="MOCK")
    assert SetupEngine().detect(context, quote, [], StrategyProfile.balanced) == []


def test_position_manager_breakeven_and_decisive_reversal_exit():
    from goldsentinel.domain import ManagementAction, Quote

    position = Position(ticket="1", symbol="XAUUSD.m", side=Side.buy, volume=.1, entry=100, current_price=100.9, stop_loss=99, take_profit=102, magic=260115, comment="GS:test")
    quote = Quote(symbol="XAUUSD.m", bid=100.9, ask=101.0, timestamp=utcnow(), source="MOCK")
    decision = PositionManager.evaluate(position, quote, _context("BULLISH"))
    assert decision.action == ManagementAction.modify_stop
    assert decision.new_stop_loss == 100
    reversal = PositionManager.evaluate(position, quote, _context("BEARISH", bos="BEARISH"))
    assert reversal.action == ManagementAction.close

    losing_quote = quote.model_copy(update={"bid": 99.5, "ask": 99.6})
    loss_exit = PositionManager.evaluate(position, losing_quote, _context("BEARISH", bos="BEARISH"))
    assert loss_exit.action == ManagementAction.close
    assert loss_exit.r_multiple < 0


def test_trailing_stop_tightens_but_never_widens():
    from goldsentinel.domain import ManagementAction, Quote

    quote = Quote(symbol="XAUUSD.m", bid=101.5, ask=101.6, timestamp=utcnow(), source="MOCK")
    position = Position(ticket="2", symbol="XAUUSD.m", side=Side.buy, volume=.1, entry=100, current_price=101.5, stop_loss=99, take_profit=103, magic=260115, comment="GS:test")
    tightened = PositionManager.evaluate(position, quote, _context("BULLISH"))
    assert tightened.action == ManagementAction.modify_stop
    assert tightened.new_stop_loss > position.stop_loss
    already_tighter = position.model_copy(update={"stop_loss": 100.5})
    held = PositionManager.evaluate(already_tighter, quote, _context("BULLISH"))
    assert held.action == ManagementAction.hold


class LifecycleRepository:
    def __init__(self):
        self.records = {}
        self.trades = {}
        self.events = []

    def setup_lifecycle(self, fingerprint):
        return self.records.get(fingerprint)

    def save_setup_lifecycle(self, fingerprint, state, candidate_id=None, broker_ticket=None, detail="", payload=None, retry_after=None, increment_attempt=False):
        previous = self.records.get(fingerprint, {"attempts": 0})
        self.records[fingerprint] = {"fingerprint": fingerprint, "state": state, "attempts": previous["attempts"] + int(increment_attempt), "candidate_id": candidate_id, "broker_ticket": broker_ticket or previous.get("broker_ticket"), "detail": detail, "payload": payload or {}, "retry_after": retry_after}

    def record_trade_lifecycle(self, ticket, candidate, actual_entry=None):
        self.trades.setdefault(ticket, candidate.setup_fingerprint)

    def audit(self, category, message, payload=None, level="INFO"):
        self.events.append((category, message, level))


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        action=DecisionAction.buy, symbol="XAUUSD.m", timeframe=Timeframe.m5,
        entry=2687.5, stop_loss=2677, take_profit=2709, volume=.1, risk_amount=100,
        risk_pct=.1, reward_risk=2, quality_score=80, news_status="NORMAL",
        rationale=["test"], spread_at_signal=.2, max_price_deviation=5,
        setup_type=SetupType.trend_continuation, setup_fingerprint="stable-fingerprint",
        expires_at=utcnow() + timedelta(minutes=5),
    )


def test_persistent_setup_lifecycle_prevents_duplicate_after_manager_restart():
    broker = MockBroker(); broker.connect()
    repository = LifecycleRepository()
    runtime = RuntimeSettings(mode=TradingMode.confirmation)
    first = ExecutionManager(broker, repository).execute_candidate(_candidate(), runtime, "persist-key")
    second = ExecutionManager(broker, repository).execute_candidate(_candidate(), runtime, "another-key")
    assert first.success and second.success
    assert first.position_ticket == second.position_ticket
    assert len(broker.positions()) == 1
    assert repository.records["stable-fingerprint"]["state"] == "FILLED"


class UncertainAfterFillBroker(MockBroker):
    def submit_market_order(self, symbol, order):
        filled = super().submit_market_order(symbol, order)
        return BrokerResult(success=False, state="UNCERTAIN", message="transport timeout", raw={"accepted": True}) if filled.success else filled


def test_uncertain_submission_reconciles_owned_position():
    broker = UncertainAfterFillBroker(); broker.connect()
    result = ExecutionManager(broker, LifecycleRepository()).execute_candidate(_candidate(), RuntimeSettings(mode=TradingMode.confirmation), "uncertain-key")
    assert result.success
    assert result.state == "FILLED"
    assert result.raw["reconciled"] is True


class RejectingBroker(MockBroker):
    def __init__(self):
        super().__init__()
        self.submissions = 0

    def submit_market_order(self, symbol, order):
        self.submissions += 1
        return BrokerResult(success=False, state="REJECTED", retcode=10016, message="Invalid stops")


def test_rejection_cools_down_then_allows_a_policy_retry():
    broker = RejectingBroker(); broker.connect()
    repository = LifecycleRepository()
    manager = ExecutionManager(broker, repository)
    runtime = RuntimeSettings(mode=TradingMode.confirmation)
    first = manager.execute_candidate(_candidate(), runtime, "reject-key")
    second = manager.execute_candidate(_candidate(), runtime, "reject-key")
    assert not first.success and not second.success
    assert broker.submissions == 1
    repository.records["stable-fingerprint"]["retry_after"] = utcnow() - timedelta(seconds=1)
    manager.execute_candidate(_candidate(), runtime, "reject-key")
    assert broker.submissions == 2


class CandleRepository:
    def __init__(self):
        self.rows = {}

    def upsert_candles(self, symbol, timeframe, candles):
        for candle in candles:
            self.rows[(symbol, timeframe, candle.timestamp)] = candle
        return len(candles)


class FixedHistoryBroker(MockBroker):
    def candles(self, symbol, timeframe, count, offset=0):
        seconds = 300
        end = datetime(2026, 1, 2, tzinfo=timezone.utc) - timedelta(seconds=seconds * offset)
        return [Candle(timestamp=end - timedelta(seconds=seconds * (count-index)), open=100, high=101, low=99, close=100.2, volume=10, complete=True) for index in range(count)]


def test_historical_refresh_is_deduplicated():
    broker = FixedHistoryBroker(); broker.connect()
    repository = CandleRepository()
    pipeline = HistoricalDataPipeline(broker, repository, "XAUUSD.m", batch_size=500)
    pipeline.refresh(Timeframe.m5, days=1)
    first = len(repository.rows)
    pipeline.refresh(Timeframe.m5, days=1)
    assert len(repository.rows) == first

