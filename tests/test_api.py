import os

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("GS_DATABASE_URL", "sqlite:///./backend/data/test.db")
os.environ.setdefault("GS_API_TOKEN", "test-token")
os.environ["GS_BROKER"] = "mock"

from goldsentinel.api import require_token, service
from goldsentinel.domain import RiskSettings, TradingMode


def test_authentication_dependency_rejects_missing_and_bad_tokens():
    with pytest.raises(HTTPException) as missing:
        require_token()
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException):
        require_token(x_api_token="wrong")
    require_token(x_api_token="test-token")


def test_risk_validation_is_server_side():
    with pytest.raises(ValidationError):
        RiskSettings(max_risk_per_trade_pct=20)


def test_cannot_arm_scanner():
    service.broker.connect()
    service.runtime.mode = TradingMode.scanner
    with pytest.raises(ValueError, match="FULL_AUTO"):
        service.set_armed(True)

