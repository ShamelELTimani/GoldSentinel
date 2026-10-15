from goldsentinel.domain import AccountSnapshot, Position, RiskRequest, RiskSettings, Side, StrategyProfile
from goldsentinel.risk import RiskManager


SPEC = {"volume_min": .01, "volume_max": 100, "volume_step": .01, "contract_size": 100}


def account(equity=100_000):
    return AccountSnapshot(balance=equity, equity=equity, free_margin=equity, margin=0, leverage=500)


def request(stop=2690, quality=100):
    return RiskRequest(side=Side.buy, entry=2700, stop_loss=stop, quality_score=quality)


def test_default_max_risk_is_two_percent():
    settings = RiskSettings()
    result = RiskManager().calculate(request(), account(), settings, StrategyProfile.opportunistic, SPEC, [])
    assert settings.max_risk_per_trade_pct == 2
    assert result.allowed
    assert result.risk_pct <= 2


def test_wider_stop_reduces_volume():
    manager = RiskManager()
    narrow = manager.calculate(request(2695), account(), RiskSettings(), StrategyProfile.opportunistic, SPEC, [])
    wide = manager.calculate(request(2680), account(), RiskSettings(), StrategyProfile.opportunistic, SPEC, [])
    assert wide.volume < narrow.volume
    assert wide.risk_pct <= 2


def test_minimum_volume_rejected_when_risk_too_large():
    spec = {**SPEC, "volume_min": 1.0}
    result = RiskManager().calculate(request(2500), account(1000), RiskSettings(max_risk_per_trade_pct=.25), StrategyProfile.balanced, spec, [])
    assert not result.allowed
    assert "minimum" in result.reason.lower()


def test_combined_exposure_limit():
    position = Position(ticket="1", symbol="XAUUSD.m", side=Side.buy, volume=1, entry=2700, current_price=2700, stop_loss=2690, take_profit=2720, initial_risk=6000)
    result = RiskManager().calculate(request(), account(), RiskSettings(), StrategyProfile.balanced, SPEC, [position])
    assert not result.allowed


def test_profiles_never_exceed_dashboard_cap():
    manager = RiskManager()
    results = [manager.calculate(request(), account(), RiskSettings(max_risk_per_trade_pct=1.25), profile, SPEC, []) for profile in StrategyProfile]
    assert all(item.risk_pct <= 1.25 for item in results)
    assert results[0].risk_pct < results[-1].risk_pct

