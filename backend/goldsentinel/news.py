from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

import httpx

from .config import Settings
from .domain import NewsEvent


FMP_CALENDAR_URL = "https://financialmodelingprep.com/stable/economic-calendar"
GOLD_RELEVANT_KEYWORDS = (
    "CPI", "CONSUMER PRICE", "PPI", "PRODUCER PRICE", "NFP", "PAYROLL", "PCE",
    "FED", "FOMC", "INTEREST RATE", "POWELL", "GDP", "UNEMPLOYMENT",
    "JOBLESS CLAIM", "RETAIL SALES", "ISM", "PMI", "CONSUMER CONFIDENCE",
    "DURABLE GOODS", "JOLTS", "ADP",
)
US_COUNTRIES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}
IMPACT_MAP = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "MODERATE": "MEDIUM", "LOW": "LOW"}


class CalendarProviderError(ValueError):
    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


class NewsEngine:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.events: list[NewsEvent] = []
        self.headlines: list[dict] = []
        self.last_error: str | None = None
        self.headline_error: str | None = None
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        # Backward-compatible alias consumed by earlier status clients/tests.
        self.last_refresh: datetime | None = None
        self.retry_at: datetime | None = None
        self.provider_state = "PENDING" if self.configured else "NOT_CONFIGURED"
        self.verified_empty = False
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._refresh_lock = asyncio.Lock()

    @property
    def provider(self) -> str:
        return self.settings.calendar_provider.lower()

    @property
    def calendar_url(self) -> str | None:
        if self.provider == "fmp":
            return self.settings.calendar_url or FMP_CALENDAR_URL
        return self.settings.calendar_url

    @property
    def configured(self) -> bool:
        if self.provider == "fmp":
            return bool(self.settings.calendar_api_key and self.calendar_url)
        return bool(self.calendar_url)

    async def refresh(self, force: bool = False) -> bool:
        """Refresh at most once per interval; return True only when HTTP was attempted."""
        async with self._refresh_lock:
            now = self._now()
            if not self.configured:
                self.provider_state = "NOT_CONFIGURED"
                self.last_error = (
                    "FMP API key is not configured"
                    if self.provider == "fmp"
                    else "Economic calendar URL is not configured"
                )
                return False
            if not self._refresh_due(now, force):
                return False

            self.last_attempt = now
            self.last_error = None
            try:
                async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                    if self.provider == "fmp":
                        response = await client.get(self.calendar_url, params=self._fmp_params(now))
                    else:
                        response = await client.get(
                            self.calendar_url,
                            headers=self._generic_headers(self.settings.calendar_api_key),
                        )
                    if not self._process_calendar_response(response, now):
                        return True

                    if self.settings.news_url:
                        try:
                            news_response = await client.get(
                                self.settings.news_url,
                                headers=self._generic_headers(self.settings.news_api_key),
                            )
                            news_response.raise_for_status()
                            self.headlines = self._parse_headlines(
                                news_response.json(), self.settings.news_url
                            )
                            self.headline_error = None
                        except Exception as exc:
                            # Headline availability does not define economic-calendar readiness.
                            self.headline_error = self._safe_error(exc)
                return True
            except CalendarProviderError as exc:
                self._record_provider_error(exc)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                self.provider_state = "API_ERROR"
                self.last_error = self._safe_error(exc)
            return True

    def _refresh_due(self, now: datetime, force: bool = False) -> bool:
        if self.retry_at and now < self.retry_at:
            self.provider_state = "RATE_LIMITED"
            return False
        return bool(
            force
            or not self.last_attempt
            or (now - self.last_attempt).total_seconds() >= self.settings.news_refresh_seconds
        )

    def _process_calendar_response(self, response: httpx.Response, now: datetime) -> bool:
        """Validate and commit one response atomically; malformed payloads never replace the cache."""
        try:
            self._validate_http_response(response, now)
            try:
                payload = response.json()
            except ValueError as exc:
                raise CalendarProviderError(
                    "INVALID_RESPONSE", "Calendar provider returned non-JSON data"
                ) from exc
            events = (
                self._parse_fmp_events(payload)
                if self.provider == "fmp"
                else self._parse_events(payload, self.calendar_url)
            )
        except CalendarProviderError as exc:
            self._record_provider_error(exc)
            return False

        self.events = sorted(events, key=lambda event: event.timestamp)
        self.verified_empty = not self.events
        self.last_success = now
        self.last_refresh = now
        self.last_error = None
        self.retry_at = None
        self.provider_state = "VERIFIED_EMPTY" if self.verified_empty else "AVAILABLE"
        return True

    def _record_provider_error(self, exc: CalendarProviderError) -> None:
        self.provider_state = exc.state
        self.last_error = str(exc)

    def _now(self) -> datetime:
        value = self._clock()
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _fmp_params(self, now: datetime) -> dict[str, str]:
        start = (now - timedelta(days=self.settings.calendar_days_back)).date()
        end = (now + timedelta(days=self.settings.calendar_days_ahead)).date()
        return {
            "apikey": self.settings.calendar_api_key or "",
            "country": self.settings.calendar_country,
            "from": start.isoformat(),
            "to": end.isoformat(),
        }

    def _validate_http_response(self, response: httpx.Response, now: datetime) -> None:
        if response.status_code in {401, 403}:
            raise CalendarProviderError(
                "PERMISSION_DENIED", "Calendar provider rejected the API key or subscription"
            )
        if response.status_code == 429:
            retry_seconds = self.settings.news_refresh_seconds
            try:
                retry_seconds = max(
                    retry_seconds, int(response.headers.get("Retry-After", retry_seconds))
                )
            except ValueError:
                pass
            self.retry_at = now + timedelta(seconds=retry_seconds)
            raise CalendarProviderError(
                "RATE_LIMITED", "Calendar provider rate limit reached; refresh is deferred"
            )
        if response.status_code >= 400:
            raise CalendarProviderError(
                "API_ERROR", f"Calendar provider returned HTTP {response.status_code}"
            )

    def _parse_fmp_events(self, payload: object) -> list[NewsEvent]:
        if isinstance(payload, dict):
            detail = " ".join(str(value) for value in payload.values()).lower()
            if any(
                term in detail
                for term in (
                    "api key", "apikey", "unauthorized", "forbidden", "subscription", "upgrade"
                )
            ):
                raise CalendarProviderError(
                    "PERMISSION_DENIED", "FMP rejected the API key or subscription"
                )
            raise CalendarProviderError(
                "INVALID_RESPONSE", "FMP economic calendar response must be a JSON array"
            )
        if not isinstance(payload, list):
            raise CalendarProviderError(
                "INVALID_RESPONSE", "FMP economic calendar response must be a JSON array"
            )

        result: list[NewsEvent] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise CalendarProviderError(
                    "INVALID_RESPONSE", f"FMP event {index} is not an object"
                )
            missing = [
                field
                for field in ("event", "date", "impact", "country", "currency")
                if not item.get(field)
            ]
            if missing:
                raise CalendarProviderError(
                    "INVALID_RESPONSE", f"FMP event {index} is missing {', '.join(missing)}"
                )

            country = str(item["country"]).strip().upper()
            currency = str(item["currency"]).strip().upper()
            title = str(item["event"]).strip()
            # The query asks FMP for US events; validate/filter the response as defense in depth.
            if country not in US_COUNTRIES or currency != "USD" or not self._gold_relevant(title):
                continue
            timestamp = self._parse_utc_timestamp(item["date"], f"FMP event {index}")
            impact = self._normalize_impact(item["impact"], f"FMP event {index}")
            actual = item.get("actual")
            forecast = item.get("estimate")
            surprise, surprise_pct = self._surprise(actual, forecast)
            identity = f"{timestamp.isoformat()}|{country}|{title}"
            result.append(
                NewsEvent(
                    id=str(item.get("id") or sha256(identity.encode()).hexdigest()[:20]),
                    title=title,
                    timestamp=timestamp,
                    impact=impact,
                    currency=currency,
                    country=country,
                    actual=actual,
                    forecast=forecast,
                    previous=item.get("previous"),
                    unit=str(item["unit"]) if item.get("unit") is not None else None,
                    surprise=surprise,
                    surprise_pct=surprise_pct,
                    source="FMP",
                )
            )
        return result

    def _parse_events(self, payload: object, source: str) -> list[NewsEvent]:
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("events", [])
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(rows, list):
            raise CalendarProviderError(
                "INVALID_RESPONSE", "Generic calendar response must be an array or {events: [...]}"
            )
        result = []
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                raise CalendarProviderError(
                    "INVALID_RESPONSE", f"Calendar event {index} is not an object"
                )
            title = str(item.get("title") or item.get("event") or "").strip()
            if not self._gold_relevant(title):
                continue
            timestamp_value = item.get("timestamp") or item.get("date")
            if not timestamp_value:
                raise CalendarProviderError(
                    "INVALID_RESPONSE", f"Calendar event {index} is missing timestamp/date"
                )
            timestamp = self._parse_utc_timestamp(timestamp_value, f"Calendar event {index}")
            impact = self._normalize_impact(item.get("impact"), f"Calendar event {index}")
            actual = item.get("actual")
            forecast = item.get("forecast")
            surprise, surprise_pct = self._surprise(actual, forecast)
            result.append(
                NewsEvent(
                    id=str(item.get("id", index)),
                    title=title,
                    timestamp=timestamp,
                    impact=impact,
                    currency=str(item.get("currency", "USD")).upper(),
                    country=item.get("country"),
                    actual=actual,
                    forecast=forecast,
                    previous=item.get("previous"),
                    unit=item.get("unit"),
                    surprise=surprise,
                    surprise_pct=surprise_pct,
                    source=source,
                )
            )
        return result

    def _parse_utc_timestamp(self, value: Any, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise CalendarProviderError(
                "INVALID_RESPONSE", f"{label} has an invalid timestamp"
            ) from exc
        # FMP documents offset-free economic-calendar timestamps as UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _normalize_impact(self, value: Any, label: str) -> str:
        impact = IMPACT_MAP.get(str(value or "").strip().upper())
        if not impact:
            raise CalendarProviderError("INVALID_RESPONSE", f"{label} has an invalid impact")
        return impact

    def _gold_relevant(self, title: str) -> bool:
        normalized = title.upper()
        return any(term in normalized for term in GOLD_RELEVANT_KEYWORDS)

    def _surprise(self, actual: Any, forecast: Any) -> tuple[float | None, float | None]:
        actual_number = self._number(actual)
        forecast_number = self._number(forecast)
        if actual_number is None or forecast_number is None:
            return None, None
        surprise = actual_number - forecast_number
        surprise_pct = (surprise / abs(forecast_number) * 100) if forecast_number else None
        return round(surprise, 6), round(surprise_pct, 4) if surprise_pct is not None else None

    def _number(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(str(value).replace(",", "").replace("%", "").strip())
        except ValueError:
            return None

    def _generic_headers(self, key: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _parse_headlines(self, payload: object, source: str) -> list[dict]:
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("articles", [])
            if isinstance(payload, dict)
            else []
        )
        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "source": item.get("source", source),
                "timestamp": item.get("publishedAt") or item.get("timestamp"),
                "fetched_at": self._now(),
            }
            for item in rows[:50]
            if isinstance(item, dict) and item.get("title")
        ]

    def _is_stale(self, now: datetime) -> bool:
        return self.last_success is None or (
            now - self.last_success
        ).total_seconds() > self.settings.news_refresh_seconds * 3

    def _available(self, now: datetime) -> bool:
        return (
            self.configured
            and self.last_error is None
            and not self._is_stale(now)
            and self.provider_state in {"AVAILABLE", "VERIFIED_EMPTY"}
        )

    def risk_context(
        self, now: datetime | None = None, policy: str = "CONSERVATIVE"
    ) -> dict:
        now = (now or self._now()).astimezone(timezone.utc)
        if not self._available(now):
            if not self.configured:
                reason = "Economic calendar is not configured"
            elif self.last_error:
                reason = self.last_error
            elif self._is_stale(now):
                reason = "Economic calendar data is stale or has not refreshed successfully"
            else:
                reason = f"Economic calendar provider is {self.provider_state.lower().replace('_', ' ')}"
            block = self.settings.news_fail_safe and policy == "CONSERVATIVE"
            return {
                "status": "UNAVAILABLE",
                "factor": 0.0 if block else 0.5,
                "block": block,
                "reason": f"{reason}; fail-safe policy applied",
            }

        high = [
            event
            for event in self.events
            if event.impact == "HIGH" and abs((event.timestamp - now).total_seconds()) <= 3600
        ]
        during = [
            event for event in high if abs((event.timestamp - now).total_seconds()) <= 10 * 60
        ]
        if during:
            return {
                "status": "EVENT_WINDOW",
                "factor": 0.0,
                "block": True,
                "reason": f"High-impact event window: {during[0].title}",
            }
        approaching = [event for event in high if now < event.timestamp <= now + timedelta(hours=1)]
        if approaching:
            factor = {"CONSERVATIVE": 0.35, "BALANCED": 0.5, "FLEXIBLE": 0.7}[policy]
            return {
                "status": "APPROACHING",
                "factor": factor,
                "block": False,
                "reason": f"High-impact event approaching: {approaching[0].title}",
            }
        return {
            "status": "NORMAL",
            "factor": 1.0,
            "block": False,
            "reason": "No known high-impact release in the strict window",
        }

    def status(self, policy: str = "CONSERVATIVE") -> dict:
        now = self._now()
        stale = self._is_stale(now)
        available = self._available(now)
        state = (
            "STALE"
            if self.configured and stale and self.last_success is not None
            else self.provider_state
        )
        next_high = next(
            (
                event
                for event in self.events
                if event.impact == "HIGH" and event.timestamp >= now
            ),
            None,
        )
        recent = [event for event in reversed(self.events) if event.timestamp < now][:4]
        restriction = self.risk_context(now, policy)
        if self.provider == "fmp":
            expected_format = {
                "root": "FMP JSON array",
                "required": ["event", "date (UTC)", "impact", "country", "currency"],
                "optional": ["estimate", "actual", "previous", "unit"],
            }
        else:
            expected_format = {
                "root": "JSON array or {events: [...]}",
                "required": ["title or event", "timestamp or date (ISO-8601)", "impact"],
                "optional": [
                    "id", "currency", "country", "actual", "forecast", "previous", "unit"
                ],
            }
        return {
            "provider": self.provider,
            "provider_state": state,
            "configured": self.configured,
            "available": available,
            "verified_empty": available and self.verified_empty,
            "fresh": available and not stale,
            "last_attempt": self.last_attempt,
            "last_success": self.last_success,
            "last_refresh": self.last_success,
            "retry_at": self.retry_at,
            "error": self.last_error,
            "headline_error": self.headline_error,
            "date_range": self._date_range(now),
            "next_high_impact": next_high,
            "seconds_to_next_high_impact": (
                max(0, int((next_high.timestamp - now).total_seconds())) if next_high else None
            ),
            "recent_releases": recent,
            "restriction": restriction,
            "expected_format": expected_format,
            "events": self.events,
            "headlines": self.headlines,
        }

    def _date_range(self, now: datetime) -> dict[str, str]:
        return {
            "country": self.settings.calendar_country,
            "from": (now - timedelta(days=self.settings.calendar_days_back)).date().isoformat(),
            "to": (now + timedelta(days=self.settings.calendar_days_ahead)).date().isoformat(),
        }

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        if self.settings.calendar_api_key:
            message = message.replace(self.settings.calendar_api_key, "[redacted]")
        return message or exc.__class__.__name__
