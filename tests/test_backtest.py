from goldsentinel.backtest import run_backtest, walk_forward
from goldsentinel.domain import StrategyProfile, Timeframe


def test_backtest_is_replay_based_and_reports_sample_limit(broker):
    candles = broker.candles("XAUUSD.m", Timeframe.m15, 500)
    result = run_backtest(candles, Timeframe.m15, StrategyProfile.balanced)
    assert result["initial_balance"] == 100_000
    assert "statistically_limited" in result
    assert all(trade["exit_time"] >= trade["entry_time"] for trade in result["trades"])


def test_walk_forward_marks_out_of_sample(broker):
    candles = broker.candles("XAUUSD.m", Timeframe.m15, 600)
    result = walk_forward(candles, Timeframe.m15, StrategyProfile.conservative, folds=3)
    assert len(result["folds"]) == 2
    assert all(fold["out_of_sample"] for fold in result["folds"])

