from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GS_", extra="ignore")

    app_name: str = "GoldSentinel"
    environment: str = "development"
    api_token: str = "change-me"
    broker: str = "mock"
    symbol: str = "XAUUSD.m"
    database_url: str = "sqlite:///./backend/data/goldsentinel.db"
    cors_origins: str = "http://localhost:5173"
    calendar_url: str | None = None
    calendar_api_key: str | None = None
    calendar_provider: str = Field(default="generic_json", pattern="^(generic_json|fmp|forex_factory)$")
    calendar_country: str = "US"
    calendar_days_back: int = Field(default=1, ge=0, le=89)
    calendar_days_ahead: int = Field(default=14, ge=0, le=89)
    news_url: str | None = None
    news_api_key: str | None = None
    news_provider: str = Field(default="generic_json", pattern="^(generic_json|rss)$")
    news_fail_safe: bool = True
    news_refresh_seconds: int = Field(default=300, ge=60, le=3600)
    headline_refresh_seconds: int = Field(default=300, ge=60, le=3600)
    polling_seconds: float = Field(default=2.0, ge=0.5, le=60)
    analysis_refresh_seconds: float = Field(default=10.0, ge=2, le=300)
    position_manage_seconds: float = Field(default=5.0, ge=2, le=300)
    history_batch_size: int = Field(default=5000, ge=100, le=10000)
    history_refresh_on_startup: bool = False
    history_m1_days: int = Field(default=14, ge=1, le=90)
    history_m5_days: int = Field(default=120, ge=1, le=730)
    history_m15_days: int = Field(default=180, ge=1, le=1095)
    history_h1_days: int = Field(default=540, ge=1, le=3650)
    history_h4_days: int = Field(default=1080, ge=1, le=7300)

    @model_validator(mode="after")
    def validate_calendar_window(self) -> "Settings":
        # FMP documents a maximum inclusive date range of 90 days.
        if self.calendar_days_back + self.calendar_days_ahead > 89:
            raise ValueError("calendar date range cannot exceed 90 inclusive days")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def ensure_directories(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            raw = self.database_url.removeprefix("sqlite:///")
            Path(raw).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings

