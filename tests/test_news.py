from datetime import datetime, timedelta, timezone

import httpx
import pytest

from goldsentinel.config import Settings
from goldsentinel.news import FMP_CALENDAR_URL, FOREX_FACTORY_CALENDAR_URL, NewsEngine


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def fmp_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "calendar_provider": "fmp",
        "calendar_api_key": "personal-secret",
        "calendar_days_back": 2,
        "calendar_days_ahead": 10,
        "news_fail_safe": True,
        "news_refresh_seconds": 300,
    }
    values.update(overrides)
    return Settings(**values)


def fmp_response(status: int, payload, headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", FMP_CALENDAR_URL)
    return httpx.Response(status, json=payload, headers=headers, request=request)


def test_unconfigured_calendar_uses_conservative_fail_safe():
    engine = NewsEngine(Settings(_env_file=None, calendar_url=None, news_fail_safe=True))
    context = engine.risk_context(policy="CONSERVATIVE")
    assert context["status"] == "UNAVAILABLE"
    assert context["block"] is True
    assert context["factor"] == 0


def test_generic_calendar_response_format_is_preserved():
    engine = NewsEngine(
        Settings(_env_file=None, calendar_url="https://calendar.invalid/events")
    )
    payload = {
        "events": [
            {
                "id": "cpi-1",
                "event": "US CPI",
                "date": "2026-10-16T12:30:00Z",
                "impact": "high",
                "currency": "USD",
                "forecast": "2.8%",
                "previous": "2.9%",
            },
            {
                "event": "Unrelated event",
                "date": "2026-10-16T13:00:00Z",
                "impact": "low",
            },
        ]
    }
    events = engine._parse_events(payload, engine.settings.calendar_url)
    assert len(events) == 1
    assert events[0].title == "US CPI"
    assert events[0].actual is None
    assert events[0].source == "https://calendar.invalid/events"


def test_fmp_query_auth_date_range_and_field_mapping():
    engine = NewsEngine(fmp_settings(), clock=lambda: NOW)
    params = engine._fmp_params(NOW)
    request = httpx.Request("GET", engine.calendar_url, params=params)
    assert request.url.params["apikey"] == "personal-secret"
    assert request.url.params["country"] == "US"
    assert request.url.params["from"] == "2026-10-13"
    assert request.url.params["to"] == "2026-10-25"
    assert engine._generic_headers(None) == {}

    payload = [
        {
            "date": "2026-10-15 12:30:00",
            "country": "US",
            "event": "Core CPI MoM",
            "currency": "USD",
            "previous": 0.2,
            "estimate": 0.3,
            "actual": 0.4,
            "impact": "High",
            "unit": "%",
        },
        {
            "date": "2026-10-15 13:00:00",
            "country": "US",
            "event": "Wholesale Inventories",
            "currency": "USD",
            "previous": 1,
            "estimate": 1,
            "actual": None,
            "impact": "Low",
        },
        {
            "date": "2026-10-15 14:00:00",
            "country": "CA",
            "event": "CPI",
            "currency": "CAD",
            "previous": 1,
            "estimate": 1,
            "actual": None,
            "impact": "High",
        },
    ]
    assert engine._process_calendar_response(fmp_response(200, payload), NOW) is True
    status = engine.status()
    assert len(engine.events) == 1
    event = engine.events[0]
    assert event.title == "Core CPI MoM"
    assert event.timestamp == datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    assert event.impact == "HIGH"
    assert event.forecast == 0.3
    assert event.actual == 0.4
    assert event.previous == 0.2
    assert event.country == "US"
    assert event.currency == "USD"
    assert event.surprise == pytest.approx(0.1)
    assert event.surprise_pct == pytest.approx(33.3333)
    assert status["provider_state"] == "AVAILABLE"
    assert status["next_high_impact"].title == "Core CPI MoM"
    assert status["seconds_to_next_high_impact"] == 1800


def test_fmp_invalid_key_and_permission_response_are_unavailable():
    for response in (
        fmp_response(401, {"error": "Invalid API key"}),
        fmp_response(200, {"Error Message": "Upgrade your subscription"}),
    ):
        engine = NewsEngine(fmp_settings(), clock=lambda: NOW)
        assert engine._process_calendar_response(response, NOW) is False
        status = engine.status()
        assert status["provider_state"] == "PERMISSION_DENIED"
        assert status["available"] is False
        assert engine.risk_context(policy="CONSERVATIVE")["block"] is True
        assert "personal-secret" not in (status["error"] or "")


def test_fmp_payment_required_is_explicit_and_fail_safe_remains_active():
    engine = NewsEngine(fmp_settings(), clock=lambda: NOW)
    assert engine._process_calendar_response(fmp_response(402, {}), NOW) is False
    status = engine.status()
    assert status["provider_state"] == "PAYMENT_REQUIRED"
    assert "paid subscription" in status["error"]
    assert engine.risk_context(policy="CONSERVATIVE")["block"] is True


def test_fmp_missing_timestamp_rejects_entire_response():
    payload = [
        {
            "country": "US",
            "event": "CPI",
            "currency": "USD",
            "estimate": 2.8,
            "impact": "High",
        }
    ]
    engine = NewsEngine(fmp_settings(), clock=lambda: NOW)
    assert engine._process_calendar_response(fmp_response(200, payload), NOW) is False
    status = engine.status()
    assert status["provider_state"] == "INVALID_RESPONSE"
    assert status["available"] is False
    assert "missing date" in status["error"]


def test_fmp_verified_empty_is_available_not_an_outage():
    engine = NewsEngine(fmp_settings(), clock=lambda: NOW)
    assert engine._process_calendar_response(fmp_response(200, []), NOW) is True
    status = engine.status()
    assert status["provider_state"] == "VERIFIED_EMPTY"
    assert status["verified_empty"] is True
    assert status["available"] is True
    assert status["events"] == []
    assert engine.risk_context(policy="CONSERVATIVE")["block"] is False


def test_fmp_outage_and_stale_cache_are_rejected():
    current = [NOW]
    engine = NewsEngine(fmp_settings(), clock=lambda: current[0])
    assert engine._process_calendar_response(fmp_response(200, []), current[0]) is True

    current[0] += timedelta(seconds=301)
    assert engine._process_calendar_response(
        fmp_response(503, {"error": "maintenance"}), current[0]
    ) is False
    assert engine.status()["provider_state"] == "API_ERROR"
    assert engine.status()["available"] is False
    assert engine.risk_context(policy="CONSERVATIVE")["block"] is True

    current[0] += timedelta(seconds=901)
    assert engine.status()["provider_state"] == "STALE"
    assert engine.status()["available"] is False


def test_fmp_refresh_cache_and_rate_limit_prevent_extra_requests():
    engine = NewsEngine(fmp_settings(), clock=lambda: NOW)
    engine.last_attempt = NOW
    assert engine._refresh_due(NOW) is False
    assert engine._refresh_due(NOW + timedelta(seconds=301)) is True

    limited_at = NOW + timedelta(seconds=301)
    assert engine._process_calendar_response(
        fmp_response(429, {}, {"Retry-After": "900"}), limited_at
    ) is False
    assert engine.provider_state == "RATE_LIMITED"
    assert engine._refresh_due(limited_at + timedelta(seconds=300), force=True) is False
    assert engine._refresh_due(limited_at + timedelta(seconds=901)) is True


def test_fmp_without_personal_key_is_not_configured():
    engine = NewsEngine(fmp_settings(calendar_api_key=None), clock=lambda: NOW)
    status = engine.status()
    assert status["configured"] is False
    assert status["provider_state"] == "NOT_CONFIGURED"
    assert status["available"] is False


def test_calendar_window_cannot_exceed_fmp_limit():
    with pytest.raises(ValueError, match="90 inclusive days"):
        fmp_settings(calendar_days_back=45, calendar_days_ahead=45)


def test_forex_factory_mapping_and_new_york_timestamp_normalization():
    engine = NewsEngine(
        Settings(_env_file=None, calendar_provider="forex_factory"),
        clock=lambda: NOW,
    )
    response = httpx.Response(
        200,
        json=[
            {
                "title": "Core CPI m/m",
                "country": "USD",
                "date": "10-15-2026 08:30am",
                "impact": "High",
                "forecast": "0.3%",
                "previous": "0.2%",
                "actual": "0.4%",
            },
            {
                "title": "Canadian CPI",
                "country": "CAD",
                "date": "10-15-2026 08:30am",
                "impact": "High",
            },
        ],
        request=httpx.Request("GET", FOREX_FACTORY_CALENDAR_URL),
    )
    assert engine._process_calendar_response(response, NOW) is True
    assert len(engine.events) == 1
    event = engine.events[0]
    assert event.timestamp == datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    assert event.currency == "USD"
    assert event.country == "US"
    assert event.actual == "0.4%"
    assert event.forecast == "0.3%"
    assert event.surprise == pytest.approx(0.1)


def test_rss_headlines_are_independent_from_calendar_failure():
    settings = fmp_settings(
        news_provider="rss", news_url="https://headlines.invalid/gold.xml"
    )
    engine = NewsEngine(settings, clock=lambda: NOW)
    assert engine._process_calendar_response(fmp_response(402, {}), NOW) is False
    rss = """<?xml version="1.0"?><rss><channel><item>
      <title>Gold rises as Federal Reserve outlook shifts</title>
      <link>https://example.invalid/gold</link>
      <source>Example Markets</source>
      <pubDate>Thu, 15 Oct 2026 11:30:00 GMT</pubDate>
    </item><item><title>Unrelated sports result</title></item></channel></rss>"""
    response = httpx.Response(
        200,
        text=rss,
        request=httpx.Request("GET", settings.news_url),
    )
    assert engine._process_headline_response(response, NOW) is True
    status = engine.status()
    assert status["provider_state"] == "PAYMENT_REQUIRED"
    assert status["headline_state"] == "AVAILABLE"
    assert len(status["headlines"]) == 1
    assert status["headlines"][0]["source"] == "Example Markets"
