from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .domain import NewsEvent


FMP_CALENDAR_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FOREX_FACTORY_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
GOLD_RELEVANT_KEYWORDS = (
    "CPI", "CONSUMER PRICE", "PPI", "PRODUCER PRICE", "NFP", "PAYROLL", "PCE",
    "FED", "FOMC", "INTEREST RATE", "POWELL", "GDP", "UNEMPLOYMENT",
    "JOBLESS CLAIM", "RETAIL SALES", "ISM", "PMI", "CONSUMER CONFIDENCE",
    "DURABLE GOODS", "JOLTS", "ADP",
)
HEADLINE_KEYWORDS = (
    "GOLD", "XAU", "DOLLAR", "USD", "FED", "FEDERAL RESERVE", "INFLATION", "CPI",
    "PCE", "PAYROLL", "TREASURY", "INTEREST RATE", "POWELL", "CENTRAL BANK",
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
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_refresh: datetime | None = None
        self.retry_at: datetime | None = None
        self.verified_empty = False
        self.headline_error: str | None = None
        self.headline_last_attempt: datetime | None = None
        self.headline_last_success: datetime | None = None
        self.headline_retry_at: datetime | None = None
        self.headline_verified_empty = False
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._refresh_lock = asyncio.Lock()
        self.provider_state = "PENDING" if self.configured else "NOT_CONFIGURED"
        self.headline_state = "PENDING" if self.headline_configured else "NOT_CONFIGURED"

    @property
    def provider(self) -> str:
        return self.settings.calendar_provider.lower()

    @property
    def headline_provider(self) -> str:
        return self.settings.news_provider.lower()

    @property
    def calendar_url(self) -> str | None:
        if self.provider == "fmp":
            return self.settings.calendar_url or FMP_CALENDAR_URL
        if self.provider == "forex_factory":
            return self.settings.calendar_url or FOREX_FACTORY_CALENDAR_URL
        return self.settings.calendar_url

    @property
    def headline_url(self) -> str | None:
        return self.settings.news_url

    @property
    def configured(self) -> bool:
        if self.provider == "fmp":
            return bool(self.settings.calendar_api_key and self.calendar_url)
        return bool(self.calendar_url)

    @property
    def headline_configured(self) -> bool:
        return bool(self.headline_url)

    async def refresh(self, force: bool = False) -> bool:
        """Refresh calendar and headlines independently under one concurrency guard."""
        async with self._refresh_lock:
            now = self._now()
            attempted = False
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                attempted = await self._refresh_calendar(client, now, force) or attempted
                attempted = await self._refresh_headlines(client, now, force) or attempted
            return attempted

    async def _refresh_calendar(
        self, client: httpx.AsyncClient, now: datetime, force: bool
    ) -> bool:
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
            if self.provider == "fmp":
                response = await client.get(self.calendar_url, params=self._fmp_params(now))
            else:
                response = await client.get(
                    self.calendar_url,
                    headers=(
                        self._generic_headers(self.settings.calendar_api_key)
                        if self.provider == "generic_json"
                        else {}
                    ),
                )
            self._process_calendar_response(response, now)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            self.provider_state = "API_ERROR"
            self.last_error = self._safe_error(exc)
        return True

    async def _refresh_headlines(
        self, client: httpx.AsyncClient, now: datetime, force: bool
    ) -> bool:
        if not self.headline_configured:
            self.headline_state = "NOT_CONFIGURED"
            self.headline_error = "Headline feed URL is not configured"
            return False
        if not self._headline_refresh_due(now, force):
            return False
        self.headline_last_attempt = now
        self.headline_error = None
        try:
            response = await client.get(
                self.headline_url,
                headers=self._generic_headers(self.settings.news_api_key),
            )
            self._process_headline_response(response, now)
        except (httpx.HTTPError, ValueError, TypeError, ET.ParseError) as exc:
            self.headline_state = "API_ERROR"
            self.headline_error = self._safe_error(exc)
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

    def _headline_refresh_due(self, now: datetime, force: bool = False) -> bool:
        if self.headline_retry_at and now < self.headline_retry_at:
            self.headline_state = "RATE_LIMITED"
            return False
        return bool(
            force
            or not self.headline_last_attempt
            or (now - self.headline_last_attempt).total_seconds()
            >= self.settings.headline_refresh_seconds
        )

    def _process_calendar_response(self, response: httpx.Response, now: datetime) -> bool:
        try:
            self._validate_http_response(response, now, calendar=True)
            try:
                payload = response.json()
            except ValueError as exc:
                raise CalendarProviderError(
                    "INVALID_RESPONSE", "Calendar provider returned non-JSON data"
                ) from exc
            if self.provider == "fmp":
                events = self._parse_fmp_events(payload)
            elif self.provider == "forex_factory":
                events = self._parse_forex_factory_events(payload)
            else:
                events = self._parse_events(payload, self.calendar_url)
        except CalendarProviderError as exc:
            self.provider_state = exc.state
            self.last_error = str(exc)
            return False
        self.events = sorted(events, key=lambda event: event.timestamp)
        self.verified_empty = not self.events
        self.last_success = now
        self.last_refresh = now
        self.last_error = None
        self.retry_at = None
        self.provider_state = "VERIFIED_EMPTY" if self.verified_empty else "AVAILABLE"
        return True

    def _process_headline_response(self, response: httpx.Response, now: datetime) -> bool:
        try:
            self._validate_http_response(response, now, calendar=False)
            if self.headline_provider == "rss":
                headlines = self._parse_rss_headlines(response.text, self.headline_url)
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CalendarProviderError(
                        "INVALID_RESPONSE", "Headline provider returned non-JSON data"
                    ) from exc
                headlines = self._parse_headlines(payload, self.headline_url)
        except (CalendarProviderError, ET.ParseError) as exc:
            self.headline_state = getattr(exc, "state", "INVALID_RESPONSE")
            self.headline_error = str(exc)
            return False
        self.headlines = headlines
        self.headline_verified_empty = not headlines
        self.headline_last_success = now
        self.headline_retry_at = None
        self.headline_error = None
        self.headline_state = "VERIFIED_EMPTY" if not headlines else "AVAILABLE"
        return True

    def _now(self) -> datetime:
        value = self._clock()
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _fmp_params(self, now: datetime) -> dict[str, str]:
        return {
            "apikey": self.settings.calendar_api_key or "",
            "country": self.settings.calendar_country,
            "from": (now - timedelta(days=self.settings.calendar_days_back)).date().isoformat(),
            "to": (now + timedelta(days=self.settings.calendar_days_ahead)).date().isoformat(),
        }

    def _validate_http_response(
        self, response: httpx.Response, now: datetime, calendar: bool = True
    ) -> None:
        label = "Calendar" if calendar else "Headline"
        if response.status_code in {401, 403}:
            raise CalendarProviderError(
                "PERMISSION_DENIED", f"{label} provider rejected the credential or subscription"
            )
        if response.status_code == 402:
            raise CalendarProviderError(
                "PAYMENT_REQUIRED", f"{label} provider requires a compatible paid subscription"
            )
        if response.status_code == 429:
            interval = (
                self.settings.news_refresh_seconds
                if calendar
                else self.settings.headline_refresh_seconds
            )
            try:
                interval = max(interval, int(response.headers.get("Retry-After", interval)))
            except ValueError:
                pass
            if calendar:
                self.retry_at = now + timedelta(seconds=interval)
            else:
                self.headline_retry_at = now + timedelta(seconds=interval)
            raise CalendarProviderError(
                "RATE_LIMITED", f"{label} provider rate limit reached; refresh is deferred"
            )
        if response.status_code >= 400:
            raise CalendarProviderError(
                "API_ERROR", f"{label} provider returned HTTP {response.status_code}"
            )

    def _parse_fmp_events(self, payload: object) -> list[NewsEvent]:
        if isinstance(payload, dict):
            detail = " ".join(str(value) for value in payload.values()).lower()
            if any(term in detail for term in ("api key", "apikey", "unauthorized", "forbidden", "subscription", "upgrade")):
                raise CalendarProviderError("PERMISSION_DENIED", "FMP rejected the API key or subscription")
            raise CalendarProviderError("INVALID_RESPONSE", "FMP economic calendar response must be a JSON array")
        if not isinstance(payload, list):
            raise CalendarProviderError("INVALID_RESPONSE", "FMP economic calendar response must be a JSON array")
        result: list[NewsEvent] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise CalendarProviderError("INVALID_RESPONSE", f"FMP event {index} is not an object")
            missing = [field for field in ("event", "date", "impact", "country", "currency") if not item.get(field)]
            if missing:
                raise CalendarProviderError("INVALID_RESPONSE", f"FMP event {index} is missing {', '.join(missing)}")
            country = str(item["country"]).strip().upper()
            currency = str(item["currency"]).strip().upper()
            title = str(item["event"]).strip()
            if country not in US_COUNTRIES or currency != "USD" or not self._gold_relevant(title):
                continue
            result.append(self._event(item, index, title, item["date"], item["impact"], currency, country, item.get("estimate"), "FMP"))
        return result

    def _parse_forex_factory_events(self, payload: object) -> list[NewsEvent]:
        if not isinstance(payload, list):
            raise CalendarProviderError("INVALID_RESPONSE", "Forex Factory calendar response must be a JSON array")
        result: list[NewsEvent] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise CalendarProviderError("INVALID_RESPONSE", f"Calendar event {index} is not an object")
            missing = [field for field in ("title", "country", "date", "impact") if not item.get(field)]
            if missing:
                raise CalendarProviderError("INVALID_RESPONSE", f"Calendar event {index} is missing {', '.join(missing)}")
            currency = str(item["country"]).strip().upper()
            title = str(item["title"]).strip()
            if currency != "USD" or not self._gold_relevant(title):
                continue
            timestamp = self._parse_forex_factory_timestamp(
                item["date"], f"Calendar event {index}"
            )
            result.append(self._event(item, index, title, timestamp.isoformat(), item["impact"], "USD", "US", item.get("forecast"), "Forex Factory"))
        return result

    def _parse_forex_factory_timestamp(self, value: Any, label: str) -> datetime:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(raw.lower(), "%m-%d-%Y %I:%M%p")
            except ValueError as exc:
                raise CalendarProviderError(
                    "INVALID_RESPONSE", f"{label} has an invalid timestamp"
                ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        return parsed.astimezone(timezone.utc)

    def _parse_events(self, payload: object, source: str) -> list[NewsEvent]:
        rows = payload if isinstance(payload, list) else payload.get("events", []) if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CalendarProviderError("INVALID_RESPONSE", "Generic calendar response must be an array or {events: [...]}")
        result: list[NewsEvent] = []
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                raise CalendarProviderError("INVALID_RESPONSE", f"Calendar event {index} is not an object")
            title = str(item.get("title") or item.get("event") or "").strip()
            if not self._gold_relevant(title):
                continue
            timestamp = item.get("timestamp") or item.get("date")
            if not timestamp:
                raise CalendarProviderError("INVALID_RESPONSE", f"Calendar event {index} is missing timestamp/date")
            result.append(self._event(item, index, title, timestamp, item.get("impact"), str(item.get("currency", "USD")).upper(), item.get("country"), item.get("forecast"), source))
        return result

    def _event(self, item: dict, index: int, title: str, timestamp_value: Any, impact_value: Any, currency: str, country: str | None, forecast: Any, source: str) -> NewsEvent:
        timestamp = self._parse_utc_timestamp(timestamp_value, f"Calendar event {index}")
        impact = self._normalize_impact(impact_value, f"Calendar event {index}")
        actual = item.get("actual")
        surprise, surprise_pct = self._surprise(actual, forecast)
        identity = f"{timestamp.isoformat()}|{currency}|{title}"
        return NewsEvent(id=str(item.get("id") or sha256(identity.encode()).hexdigest()[:20]), title=title, timestamp=timestamp, impact=impact, currency=currency, country=country, actual=actual, forecast=forecast, previous=item.get("previous"), unit=str(item["unit"]) if item.get("unit") is not None else None, surprise=surprise, surprise_pct=surprise_pct, source=source)

    def _parse_utc_timestamp(self, value: Any, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise CalendarProviderError("INVALID_RESPONSE", f"{label} has an invalid timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _normalize_impact(self, value: Any, label: str) -> str:
        impact = IMPACT_MAP.get(str(value or "").strip().upper())
        if not impact:
            raise CalendarProviderError("INVALID_RESPONSE", f"{label} has an invalid impact")
        return impact

    def _gold_relevant(self, title: str) -> bool:
        return any(term in title.upper() for term in GOLD_RELEVANT_KEYWORDS)

    def _surprise(self, actual: Any, forecast: Any) -> tuple[float | None, float | None]:
        actual_number, forecast_number = self._number(actual), self._number(forecast)
        if actual_number is None or forecast_number is None:
            return None, None
        surprise = actual_number - forecast_number
        percentage = surprise / abs(forecast_number) * 100 if forecast_number else None
        return round(surprise, 6), round(percentage, 4) if percentage is not None else None

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
        rows = payload if isinstance(payload, list) else payload.get("articles", []) if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CalendarProviderError("INVALID_RESPONSE", "Headline response must be an array or {articles: [...]}")
        return [self._headline(item.get("title"), item.get("url"), item.get("source", source), item.get("publishedAt") or item.get("timestamp")) for item in rows[:50] if isinstance(item, dict) and self._headline_relevant(str(item.get("title") or ""))]

    def _parse_rss_headlines(self, payload: str, source: str) -> list[dict]:
        root = ET.fromstring(payload)
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        result = []
        for item in items[:50]:
            title = self._xml_text(item, "title")
            if not title or not self._headline_relevant(title):
                continue
            link = self._xml_text(item, "link")
            if not link:
                link_node = item.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.get("href") if link_node is not None else None
            published = self._xml_text(item, "pubDate") or self._xml_text(item, "published") or self._xml_text(item, "updated")
            source_name = self._xml_text(item, "source") or source
            result.append(self._headline(title, link, source_name, published))
        return result

    def _xml_text(self, item: ET.Element, name: str) -> str | None:
        node = item.find(name)
        if node is None:
            node = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
        return node.text.strip() if node is not None and node.text else None

    def _headline_relevant(self, title: str) -> bool:
        return any(term in title.upper() for term in HEADLINE_KEYWORDS)

    def _headline(self, title: str, url: str | None, source: Any, published: Any) -> dict:
        timestamp = None
        if published:
            try:
                timestamp = parsedate_to_datetime(str(published))
            except (TypeError, ValueError):
                try:
                    timestamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                except ValueError:
                    timestamp = None
        if timestamp and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        source_name = source if isinstance(source, str) else source.get("name") if isinstance(source, dict) else str(source)
        return {"title": title, "url": url, "source": source_name, "timestamp": timestamp.astimezone(timezone.utc) if timestamp else None, "fetched_at": self._now()}

    def _is_stale(self, now: datetime) -> bool:
        return self.last_success is None or (now - self.last_success).total_seconds() > self.settings.news_refresh_seconds * 3

    def _headline_is_stale(self, now: datetime) -> bool:
        return self.headline_last_success is None or (now - self.headline_last_success).total_seconds() > self.settings.headline_refresh_seconds * 3

    def _available(self, now: datetime) -> bool:
        return self.configured and self.last_error is None and not self._is_stale(now) and self.provider_state in {"AVAILABLE", "VERIFIED_EMPTY"}

    def risk_context(self, now: datetime | None = None, policy: str = "CONSERVATIVE") -> dict:
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
            return {"status": "UNAVAILABLE", "factor": 0.0 if block else 0.5, "block": block, "reason": f"{reason}; fail-safe policy applied"}
        high = [event for event in self.events if event.impact == "HIGH" and abs((event.timestamp - now).total_seconds()) <= 3600]
        during = [event for event in high if abs((event.timestamp - now).total_seconds()) <= 600]
        if during:
            return {"status": "EVENT_WINDOW", "factor": 0.0, "block": True, "reason": f"High-impact event window: {during[0].title}"}
        approaching = [event for event in high if now < event.timestamp <= now + timedelta(hours=1)]
        if approaching:
            factor = {"CONSERVATIVE": 0.35, "BALANCED": 0.5, "FLEXIBLE": 0.7}[policy]
            return {"status": "APPROACHING", "factor": factor, "block": False, "reason": f"High-impact event approaching: {approaching[0].title}"}
        return {"status": "NORMAL", "factor": 1.0, "block": False, "reason": "No known high-impact release in the strict window"}

    def status(self, policy: str = "CONSERVATIVE") -> dict:
        now = self._now()
        available = self._available(now)
        headline_available = self.headline_configured and self.headline_error is None and not self._headline_is_stale(now) and self.headline_state in {"AVAILABLE", "VERIFIED_EMPTY"}
        state = "STALE" if self.configured and self._is_stale(now) and self.last_success else self.provider_state
        headline_state = "STALE" if self.headline_configured and self._headline_is_stale(now) and self.headline_last_success else self.headline_state
        next_high = next((event for event in self.events if event.impact == "HIGH" and event.timestamp >= now), None)
        restriction = self.risk_context(now, policy)
        return {
            "provider": self.provider, "provider_state": state, "configured": self.configured,
            "available": available, "verified_empty": available and self.verified_empty,
            "fresh": available and not self._is_stale(now), "last_attempt": self.last_attempt,
            "last_success": self.last_success, "last_refresh": self.last_success,
            "retry_at": self.retry_at, "error": self.last_error, "date_range": self._date_range(now),
            "next_high_impact": next_high,
            "seconds_to_next_high_impact": max(0, int((next_high.timestamp - now).total_seconds())) if next_high else None,
            "recent_releases": [event for event in reversed(self.events) if event.timestamp < now][:4],
            "restriction": restriction, "events": self.events,
            "headline_provider": self.headline_provider, "headline_state": headline_state,
            "headline_configured": self.headline_configured, "headline_available": headline_available,
            "headline_verified_empty": headline_available and self.headline_verified_empty,
            "headline_last_attempt": self.headline_last_attempt,
            "headline_last_success": self.headline_last_success,
            "headline_retry_at": self.headline_retry_at, "headline_error": self.headline_error,
            "headlines": self.headlines,
        }

    def _date_range(self, now: datetime) -> dict[str, str]:
        return {"country": self.settings.calendar_country, "from": (now - timedelta(days=self.settings.calendar_days_back)).date().isoformat(), "to": (now + timedelta(days=self.settings.calendar_days_ahead)).date().isoformat()}

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        for secret in (self.settings.calendar_api_key, self.settings.news_api_key):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message or exc.__class__.__name__
