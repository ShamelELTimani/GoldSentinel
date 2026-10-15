from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TradingMode(str, Enum):
    full_auto = "FULL_AUTO"
    confirmation = "CONFIRMATION"
    scanner = "SCANNER"


class StrategyProfile(str, Enum):
    conservative = "CONSERVATIVE"
    balanced = "BALANCED"
    opportunistic = "OPPORTUNISTIC"


class Side(str, Enum):
    buy = "BUY"
    sell = "SELL"


class DecisionAction(str, Enum):
    buy = "BUY"
    sell = "SELL"
    no_trade = "NO_TRADE"


class DecisionClass(str, Enum):
    trade = "TRADE"
    market = "MARKET"
    risk = "RISK"
    operational = "OPERATIONAL"


class Timeframe(str, Enum):
    m1 = "M1"
    m5 = "M5"
    m15 = "M15"
    m30 = "M30"
    h1 = "H1"
    h4 = "H4"
    d1 = "D1"
    w1 = "W1"
    mn1 = "MN1"


TIMEFRAME_SECONDS = {
    Timeframe.m1: 60,
    Timeframe.m5: 300,
    Timeframe.m15: 900,
    Timeframe.m30: 1800,
    Timeframe.h1: 3600,
    Timeframe.h4: 14400,
    Timeframe.d1: 86400,
    Timeframe.w1: 604800,
    Timeframe.mn1: 2592000,
}


class Candle(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0
    spread: float = 0
    complete: bool = True

    @model_validator(mode="after")
    def valid_ohlc(self) -> "Candle":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        return self


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    source: str

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class AccountSnapshot(BaseModel):
    balance: float
    equity: float
    free_margin: float
    margin: float
    leverage: int
    floating_pnl: float = 0
    currency: str = "USD"
    is_demo: bool = True
    timestamp: datetime = Field(default_factory=utcnow)


class Zone(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    direction: Side
    timeframe: Timeframe
    lower: float
    upper: float
    invalidation: float
    entry: float
    stop_loss: float
    take_profit: float
    reward_risk: float
    quality_score: float = Field(ge=0, le=100)
    touches: int = 0
    broken: bool = False
    relevance: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    explanation: str
    created_at: datetime = Field(default_factory=utcnow)


class NewsEvent(BaseModel):
    id: str
    title: str
    timestamp: datetime
    impact: str
    currency: str = "USD"
    country: str | None = None
    actual: float | str | None = None
    forecast: float | str | None = None
    previous: float | str | None = None
    unit: str | None = None
    surprise: float | None = None
    surprise_pct: float | None = None
    source: str
    fetched_at: datetime = Field(default_factory=utcnow)


class RiskSettings(BaseModel):
    max_risk_per_trade_pct: float = Field(default=2.0, ge=0.25, le=5.0)
    max_combined_risk_pct: float = Field(default=6.0, ge=0.5, le=20)
    max_positions: int = Field(default=3, ge=1, le=20)
    daily_drawdown_stop_pct: float = Field(default=6.0, ge=0.5, le=25)
    consecutive_loss_limit: int = Field(default=4, ge=1, le=20)
    cooldown_minutes: int = Field(default=60, ge=1, le=1440)
    preferred_min_rr: float = Field(default=1.5, ge=0.5, le=10)
    news_policy: str = Field(default="CONSERVATIVE", pattern="^(CONSERVATIVE|BALANCED|FLEXIBLE)$")


class RuntimeSettings(BaseModel):
    mode: TradingMode = TradingMode.scanner
    strategy_profile: StrategyProfile = StrategyProfile.balanced
    risk: RiskSettings = Field(default_factory=RiskSettings)
    paused: bool = False
    emergency_stop: bool = False
    armed: bool = False


class RiskRequest(BaseModel):
    side: Side
    entry: float
    stop_loss: float
    quality_score: float = Field(ge=0, le=100)
    volatility_factor: float = Field(default=1, ge=0.25, le=3)
    news_factor: float = Field(default=1, ge=0, le=1)
    performance_factor: float = Field(default=1, ge=0.5, le=1.1)
    setup_factor: float = Field(default=1, ge=0.25, le=1)


class RiskResult(BaseModel):
    allowed: bool
    reason: str
    risk_pct: float = 0
    risk_amount: float = 0
    volume: float = 0
    stop_distance: float = 0
    remaining_risk_pct: float = 0


class TradeCandidate(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    action: DecisionAction
    symbol: str
    timeframe: Timeframe
    entry: float
    stop_loss: float
    take_profit: float
    volume: float
    risk_amount: float
    risk_pct: float
    reward_risk: float
    quality_score: float
    zone_id: str | None = None
    news_status: str
    rationale: list[str]
    decision_class: DecisionClass = DecisionClass.trade
    blockers: list[str] = Field(default_factory=list)
    spread_at_signal: float = 0
    max_price_deviation: float = 0
    evaluated_zones: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    status: str = "PROPOSED"


class OrderRequest(BaseModel):
    side: Side
    volume: float = Field(gt=0)
    entry: float
    stop_loss: float
    take_profit: float
    client_order_id: str = Field(default_factory=lambda: str(uuid4()))
    candidate_id: str | None = None
    max_price_deviation: float | None = Field(default=None, gt=0)


class BrokerResult(BaseModel):
    success: bool
    state: str
    retcode: int | None = None
    message: str
    order_ticket: str | None = None
    deal_ticket: str | None = None
    position_ticket: str | None = None
    filled_volume: float = 0
    raw: dict[str, Any] = Field(default_factory=dict)


class Position(BaseModel):
    ticket: str
    symbol: str
    side: Side
    volume: float
    entry: float
    current_price: float
    stop_loss: float
    take_profit: float
    pnl: float = 0
    initial_risk: float = 0
    opened_at: datetime = Field(default_factory=utcnow)

