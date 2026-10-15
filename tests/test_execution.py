from datetime import timedelta

from goldsentinel.database import Repository
from goldsentinel.domain import DecisionAction, RuntimeSettings, Side, Timeframe, TradeCandidate, TradingMode, utcnow
from goldsentinel.execution import ExecutionManager


def candidate(entry=2687.5, max_price_deviation=5):
    return TradeCandidate(action=DecisionAction.buy, symbol="XAUUSD.m", timeframe=Timeframe.m15, entry=entry, stop_loss=2677, take_profit=2709, volume=.1, risk_amount=100, risk_pct=.1, reward_risk=2, quality_score=80, news_status="NORMAL", rationale=["test"], spread_at_signal=.2, max_price_deviation=max_price_deviation, expires_at=utcnow()+timedelta(minutes=5))


def test_mode_and_arm_separation(broker, tmp_path, monkeypatch):
    runtime = RuntimeSettings(mode=TradingMode.scanner, armed=False)
    manager = ExecutionManager(broker, Repository())
    result = manager.execute_candidate(candidate(), runtime, "scanner-key")
    # Direct execution is allowed for confirmation/manual flow but safety state is still enforced.
    assert result.success
    runtime.paused = True
    assert not manager.execute_candidate(candidate(), runtime, "paused-key").success


def test_duplicate_submission_is_idempotent(broker):
    manager = ExecutionManager(broker, Repository())
    runtime = RuntimeSettings(mode=TradingMode.confirmation)
    first = manager.execute_candidate(candidate(), runtime, "same-order-key")
    second = manager.execute_candidate(candidate(), runtime, "same-order-key")
    assert first.position_ticket == second.position_ticket
    assert len(broker.positions()) == 1


def test_broker_rejects_stop_widening(broker):
    result = broker.submit_market_order("XAUUSD.m", __import__('goldsentinel.domain', fromlist=['OrderRequest']).OrderRequest(side=Side.buy, volume=.1, entry=2700, stop_loss=2690, take_profit=2720))
    modified = broker.modify_position(result.position_ticket, 2680, 2720)
    assert not modified.success


def test_execution_rejects_price_outside_approved_slippage(broker):
    manager = ExecutionManager(broker, Repository())
    result = manager.execute_candidate(
        candidate(entry=2700, max_price_deviation=.1),
        RuntimeSettings(mode=TradingMode.confirmation),
        "stale-signal-price",
    )
    assert not result.success
    assert "fresh analysis is required" in result.message
    assert broker.positions() == []

