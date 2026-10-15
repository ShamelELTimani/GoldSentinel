#!/usr/bin/env python3
"""Read-only, local GoldSentinel monitor. Python 3.11+, standard library only.

Monitors the published /api/status, /api/market/M15 and /api/logs endpoints.
Does not send POST/PUT/DELETE or collect tokens/credentials.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now_utc().isoformat(timespec="seconds")


def as_datetime(raw: object) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_seconds(raw: object) -> float | None:
    when = as_datetime(raw)
    return max(0.0, (now_utc() - when).total_seconds()) if when else None


def number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_text(value: object, max_len: int = 250) -> str:
    return str(value if value is not None else "").replace("\r", " ").replace("\n", " ")[:max_len]


def get_json(base_url: str, path: str, timeout: float) -> object:
    url = base_url.rstrip("/") + path
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "GoldSentinel-LocalMonitor/1.0"}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


class Monitor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output = Path(args.output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.run_start = stamp()
        self.running = True
        self.counts = Counter()
        self.reject_reasons = Counter()
        self.last_state: dict[str, object] = {}
        self.last_status: dict = {}
        self.last_market: dict = {}
        self.last_error: str = ""
        self.seen_log_id: int | None = None
        self.last_event_signature: tuple | None = None
        self.last_market_fetch = 0.0
        self.last_logs_fetch = 0.0
        self.last_report = 0.0
        self.sample_count = 0
        self.pos_tickets: set[str] | None = None
        self.market_count = 0
        self.price_updates = 0
        self.tick_timestamp_updates = 0
        self.last_bid_ask = None
        self.last_quote_time = None
        self.last_price_change_time = None
        self.latest_approx_position_risk = 0.0
        self.latest_position_risk_unknown = False
        self.last_zone_key: tuple | None = None

    def file(self, name: str) -> Path:
        folder = self.output / datetime.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{name}.jsonl"

    def append(self, name: str, data: dict) -> None:
        with self.file(name).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(data, default=str, ensure_ascii=False, separators=(",", ":")) + "\n")

    def event(self, kind: str, message: str, severity: str = "INFO", **details: object) -> None:
        item = {"time": stamp(), "severity": severity, "kind": kind, "message": safe_text(message, 500), **details}
        self.append("events", item)
        self.counts[f"alert_{severity}"] += 1
        print(f"  [{severity}] {kind}: {item['message']}", flush=True)

    def transition(self, name: str, value: object, description: str, severity: str = "INFO") -> None:
        # Log initial conditions as well as changes; don't spam on every poll.
        if name not in self.last_state or self.last_state[name] != value:
            old = self.last_state.get(name, "<initial>")
            self.last_state[name] = value
            self.event("STATE", f"{description}: {safe_text(old)} -> {safe_text(value)}", severity, field=name)

    def status_once(self) -> None:
        data = get_json(self.args.url, "/api/status", self.args.timeout)
        if not isinstance(data, dict):
            raise ValueError("/api/status did not return a JSON object")
        self.last_status = data
        self.sample_count += 1
        self.counts["status_success"] += 1
        self.last_error = ""

        runtime = data.get("runtime") or {}
        quote = data.get("quote") or {}
        account = data.get("account") or {}
        news = data.get("news") or {}
        risk = runtime.get("risk") or {}
        latest = data.get("latest_candidate") or {}
        positions = data.get("positions") or []
        readiness = data.get("readiness") or {}
        blockers = readiness.get("blockers") or []
        bid, ask = number(quote.get("bid")), number(quote.get("ask"))
        spread = number(data.get("spread"), ask - bid)
        prices = (bid, ask)
        if self.last_bid_ask is not None and self.last_bid_ask != prices:
            self.price_updates += 1
            self.last_price_change_time = stamp()
        self.last_bid_ask = prices
        if self.last_quote_time is not None and self.last_quote_time != quote.get("timestamp"):
            self.tick_timestamp_updates += 1
        self.last_quote_time = quote.get("timestamp")
        tick_age = age_seconds(quote.get("timestamp"))
        equity = number(account.get("equity"))

        self.transition("api_connection", "UP", "Local backend API", "INFO")
        self.transition("mt5", bool(data.get("connected")), "Broker connection", "WARNING" if not data.get("connected") else "INFO")
        self.transition("demo", bool(account.get("is_demo")), "DEMO account confirmed", "ERROR" if not account.get("is_demo") else "INFO")
        self.transition("readiness", bool(readiness.get("ready")), "Trading readiness", "WARNING" if not readiness.get("ready") else "INFO")
        self.transition("blockers", tuple(sorted(map(str, blockers))), "Readiness blockers", "WARNING" if blockers else "INFO")
        self.transition("armed", bool(runtime.get("armed")), "Auto armed", "INFO")
        self.transition("paused", bool(runtime.get("paused")), "Trading paused", "WARNING" if runtime.get("paused") else "INFO")
        self.transition("emergency", bool(runtime.get("emergency_stop")), "Emergency stop", "WARNING" if runtime.get("emergency_stop") else "INFO")
        self.transition("strategy", runtime.get("strategy_profile"), "Strategy profile")
        self.transition("mode", runtime.get("mode"), "Trading mode")
        self.transition("news_state", (news.get("provider_state"), news.get("error")), "Economic calendar provider", "WARNING" if not news.get("available") else "INFO")
        self.transition("headline_error", news.get("headline_error"), "Headline provider error", "WARNING" if news.get("headline_error") else "INFO")
        headlines = news.get("headlines") or []
        self.transition("headlines_loaded", bool(headlines), "Financial headlines loaded", "WARNING" if not headlines else "INFO")
        self.transition("news_restriction", (news.get("restriction") or {}).get("status"), "News restriction")
        self.transition("stale_tick", tick_age is None or tick_age > self.args.stale_seconds,
                        "Quote potentially stale (may also be market closure)", "WARNING" if tick_age is None or tick_age > self.args.stale_seconds else "INFO")
        self.transition("high_spread", spread > self.args.max_spread,
                        f"Spread exceeds configured {self.args.max_spread:.2f} USD", "WARNING" if spread > self.args.max_spread else "INFO")
        self.transition("quote_valid", bid > 0 and ask >= bid, "Bid/ask validity", "ERROR" if bid <= 0 or ask < bid else "INFO")
        new_tickets = {str(p.get("ticket")) for p in positions if isinstance(p, dict) and p.get("ticket") is not None}
        if self.pos_tickets is not None:
            for ticket in sorted(new_tickets - self.pos_tickets):
                self.event("POSITION_OPEN", f"New broker position {ticket}", "WARNING", ticket=ticket)
            for ticket in sorted(self.pos_tickets - new_tickets):
                self.event("POSITION_CLOSED", f"Broker position {ticket} no longer open", "INFO", ticket=ticket)
        self.pos_tickets = new_tickets

        # This is an independent approximation, not a broker-guaranteed loss limit.
        # The MT5 adapter currently reports initial_risk=0 for opened positions.
        stop_risk_estimate = 0.0
        unknown_stop = False
        for p in positions:
            if not isinstance(p, dict):
                continue
            entry, stop = number(p.get("entry")), number(p.get("stop_loss"))
            side = str(p.get("side", "")).upper()
            valid_stop = stop > 0 and ((side == "BUY" and stop < entry) or (side == "SELL" and stop > entry))
            if valid_stop:
                stop_risk_estimate += abs(entry - stop) * number(p.get("volume")) * self.args.contract_size
            else:
                unknown_stop = True
        self.latest_approx_position_risk = stop_risk_estimate
        self.latest_position_risk_unknown = unknown_stop
        self.transition("estimated_combined_over_cap", (
            bool(positions) and equity > 0 and stop_risk_estimate / equity * 100 > number(risk.get("max_combined_risk_pct"), 6.0)
        ), "Approximate open-stop risk exceeds combined exposure cap", "ERROR" if (
            bool(positions) and equity > 0 and stop_risk_estimate / equity * 100 > number(risk.get("max_combined_risk_pct"), 6.0)
        ) else "INFO")
        self.transition("missing_stop", unknown_stop, "Open position with absent or invalid stop loss", "ERROR" if unknown_stop else "INFO")
        if positions and number(data.get("open_risk")) == 0 and stop_risk_estimate > 0:
            self.transition("risk_undercount", True, "Backend risk tracking may underestimate open exposure", "WARNING")
        else:
            self.transition("risk_undercount", False, "Backend risk tracking may underestimate open exposure")
        candidate_summary = {
            "action": latest.get("action"), "decision_class": latest.get("decision_class"),
            "status": latest.get("status"), "quality_score": latest.get("quality_score"),
            "zone_id": latest.get("zone_id"), "reason": safe_text((latest.get("rationale") or [""])[-1]),
            "blockers": latest.get("blockers") or [],
        }
        candidate_signature = (candidate_summary["action"], candidate_summary["decision_class"],
                               candidate_summary["reason"], candidate_summary["zone_id"], candidate_summary["status"])
        if candidate_signature != self.last_event_signature and latest:
            self.last_event_signature = candidate_signature
            self.event("DECISION", f"{candidate_summary['action']} / {candidate_summary['decision_class']}: {candidate_summary['reason']}",
                       "WARNING" if candidate_summary["action"] not in (None, "NO_TRADE") else "INFO", candidate=candidate_summary)

        sample = {
            "time": stamp(), "broker": data.get("broker"), "symbol": data.get("symbol"),
            "bid": bid, "ask": ask, "spread": spread, "tick_timestamp": quote.get("timestamp"),
            "tick_age_seconds": round(tick_age, 1) if tick_age is not None else None,
            "connected": bool(data.get("connected")), "demo": account.get("is_demo"),
            "equity": equity, "balance": account.get("balance"), "free_margin": account.get("free_margin"),
            "runtime": {"mode": runtime.get("mode"), "profile": runtime.get("strategy_profile"),
                        "armed": runtime.get("armed"), "paused": runtime.get("paused"), "emergency_stop": runtime.get("emergency_stop"),
                        "risk_limit_pct": risk.get("max_risk_per_trade_pct"), "combined_risk_limit_pct": risk.get("max_combined_risk_pct")},
            "readiness": {"ready": readiness.get("ready"), "blockers": blockers},
            "candidate": candidate_summary, "open_positions": positions, "pending_orders_count": len(data.get("pending_orders") or []),
            "backend_open_risk": data.get("open_risk"), "approx_open_risk": round(stop_risk_estimate, 2),
            "risk_estimate_incomplete": unknown_stop, "news": {
                "calendar_provider": news.get("provider"), "calendar_state": news.get("provider_state"),
                "calendar_error": news.get("error"), "calendar_available": news.get("available"),
                "events_count": len(news.get("events") or []), "headline_count": len(headlines),
                "headline_error": news.get("headline_error"), "news_status": (news.get("restriction") or {}).get("status"),
                "news_factor": (news.get("restriction") or {}).get("factor"),
            },
        }
        self.append("snapshots", sample)
        tick_label = "?" if tick_age is None else f"{tick_age:.0f}s"
        decision = latest.get("action") or "PENDING"
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {data.get('symbol','?')} "
              f"{bid:.2f}/{ask:.2f} spread={spread:.2f} tickAge={tick_label} "
              f"ready={readiness.get('ready')} armed={runtime.get('armed')} "
              f"decision={decision} positions={len(positions)} calendar={news.get('provider_state')}", flush=True)

    def market_once(self) -> None:
        data = get_json(self.args.url, "/api/market/M15?count=350", self.args.timeout)
        if not isinstance(data, dict):
            raise ValueError("/api/market/M15 did not return a JSON object")
        self.last_market = data
        self.market_count += 1
        quality = data.get("quality") or {}
        zones = data.get("zones") or []
        self.transition("m15_quality", bool(quality.get("valid")), "M15 candle quality", "WARNING" if not quality.get("valid") else "INFO")
        self.transition("m15_errors", tuple(quality.get("errors") or []), "M15 candle errors", "WARNING" if quality.get("errors") else "INFO")
        zone_keys = tuple(sorted(str(z.get("id")) for z in zones if isinstance(z, dict)))
        if self.last_zone_key is None or zone_keys != self.last_zone_key:
            self.last_zone_key = zone_keys
            self.event("ZONES_CHANGED", f"{len(zones)} active M15 zones; {len([z for z in zones if z.get('direction')=='BUY'])} BUY, {len([z for z in zones if z.get('direction')=='SELL'])} SELL", "INFO")
        snapshot = {"time": stamp(), "symbol": data.get("symbol"), "timeframe": "M15", "structure": data.get("structure"),
                    "atr": (data.get("indicators") or {}).get("latest", {}).get("atr"),
                    "rsi": (data.get("indicators") or {}).get("latest", {}).get("rsi"),
                    "quality": {"valid": quality.get("valid"), "stale": quality.get("stale"), "errors": quality.get("errors"),
                                "age_seconds": quality.get("age_seconds"), "last_timestamp": quality.get("last_timestamp")},
                    "zones": zones}
        self.append("market", snapshot)

    def logs_once(self) -> None:
        data = get_json(self.args.url, "/api/logs?limit=200", self.args.timeout)
        if not isinstance(data, list):
            raise ValueError("/api/logs did not return a JSON array")
        audit = sorted((event for event in data if isinstance(event, dict) and isinstance(event.get("id"), int)), key=lambda x: x["id"])
        if not audit:
            return
        if self.seen_log_id is None:
            self.seen_log_id = audit[-1]["id"]
            self.event("AUDIT_START", f"Audit tracking starts after existing ID {self.seen_log_id}; historical activity is not replayed")
            return
        if audit[0]["id"] > self.seen_log_id + 1:
            self.event("AUDIT_GAP", "Some audit events may have been missed between polls", "WARNING",
                       previous_id=self.seen_log_id, first_available_id=audit[0]["id"])
        new_events = [e for e in audit if e["id"] > self.seen_log_id]
        for e in new_events:
            self.seen_log_id = e["id"]
            cat = str(e.get("category", ""))
            level = str(e.get("level", "INFO"))
            payload = e.get("payload") or {}
            if cat == "SIGNAL":
                action = payload.get("action", e.get("message", "")) if isinstance(payload, dict) else e.get("message", "")
                decision_class = payload.get("decision_class", "") if isinstance(payload, dict) else ""
                self.counts["signals_all"] += 1
                self.counts[f"signals_{action}"] += 1
                if action == "NO_TRADE":
                    last_reasons = (payload.get("rationale") or []) if isinstance(payload, dict) else []
                    reason = safe_text(last_reasons[-1] if last_reasons else "unspecified")
                    self.reject_reasons[reason] += 1
                else:
                    self.event("SIGNAL", f"{action} ({decision_class}) zone={payload.get('zone_id')}", "WARNING",
                               log_id=e["id"], action=action, decision_class=decision_class, zone_id=payload.get("zone_id"),
                               status=payload.get("status"), reason=safe_text((payload.get("rationale") or [""])[-1]))
            else:
                self.counts[f"audit_{cat}"] += 1
                if cat in {"ORDER_RESULT", "ORDER_REJECTED", "ORDER_SUBMITTING", "MANUAL_ORDER", "SYSTEM", "SAFETY", "CONFIG"} or level.upper() in {"ERROR", "WARNING", "CRITICAL"}:
                    self.event(f"AUDIT_{cat}", safe_text(e.get("message")),
                               "ERROR" if level.upper() in {"ERROR", "CRITICAL"} else "WARNING" if cat in {"ORDER_REJECTED"} or level.upper() == "WARNING" else "INFO",
                               log_id=e["id"], backend_timestamp=e.get("timestamp"), payload=payload)

    def report(self) -> None:
        s = self.last_status
        quote = s.get("quote") or {}
        risk = (s.get("runtime") or {}).get("risk") or {}
        news = s.get("news") or {}
        last = s.get("latest_candidate") or {}
        readiness = s.get("readiness") or {}
        report = [
            "GOLDSENTINEL READ-ONLY MONITOR — LATEST REPORT",
            f"Generated UTC: {stamp()}",
            f"Monitoring started UTC: {self.run_start}",
            f"API: {self.args.url} (local GET requests only)",
            f"Samples collected: {self.sample_count}; market scans: {self.market_count}",
            f"Last API error: {self.last_error or 'none'}",
            "",
            "BROKER & PRICES",
            f"Broker: {s.get('broker')} | symbol: {s.get('symbol')} | connected: {s.get('connected')}",
            f"Bid/Ask: {quote.get('bid')} / {quote.get('ask')} | quote timestamp: {quote.get('timestamp')}",
            f"Quote age: {age_seconds(quote.get('timestamp'))} seconds (old ticks are possible during market closures)",
            f"Observed price changes since start: {self.price_updates}; quote timestamp changes: {self.tick_timestamp_updates}",
            f"Most recent observed price change: {self.last_price_change_time or 'not observed yet'}",
            f"Equity: {(s.get('account') or {}).get('equity')} | DEMO: {(s.get('account') or {}).get('is_demo')}",
            "",
            "SAFETY & RISK",
            f"Runtime: {s.get('runtime')}",
            f"Readiness: {readiness.get('ready')} | blockers: {readiness.get('blockers')}",
            f"Open positions: {len(s.get('positions') or [])} | pending orders: {len(s.get('pending_orders') or [])}",
            f"Backend reported open risk: {s.get('open_risk')} (may be understated for MT5 positions)",
            f"Independent APPROXIMATE stop-based open risk: {self.latest_approx_position_risk:.2f} USD",
            f"Risk estimate incomplete because of missing/invalid SL: {self.latest_position_risk_unknown}",
            f"Per-trade max risk: {risk.get('max_risk_per_trade_pct')}% | combined max risk: {risk.get('max_combined_risk_pct')}%",
            "",
            "DECISIONS & ORDERS",
            f"Latest decision: {last.get('action')} / {last.get('decision_class')} / {last.get('status')}",
            f"Latest rationale: {safe_text((last.get('rationale') or [''])[-1], 400)}",
            f"Audit signal count since monitoring began: {self.counts['signals_all']}",
            f"BUY signals: {self.counts['signals_BUY']}; SELL signals: {self.counts['signals_SELL']}; NO_TRADE: {self.counts['signals_NO_TRADE']}",
            f"Order submissions: {self.counts['audit_ORDER_SUBMITTING']} | order results: {self.counts['audit_ORDER_RESULT']} | preflight rejections: {self.counts['audit_ORDER_REJECTED']}",
            "Top NO_TRADE explanations (since monitoring started):",
        ]
        report.extend(f"  {v:5d}  {safe_text(k, 220)}" for k, v in self.reject_reasons.most_common(8))
        if not self.reject_reasons:
            report.append("  None captured yet")
        report.extend([
            "",
            "M15 ZONES & MARKET QUALITY",
            f"Market quality: {(self.last_market.get('quality') or {}).get('valid')} | structure: {(self.last_market.get('structure') or {}).get('trend')}",
            f"ATR: {((self.last_market.get('indicators') or {}).get('latest') or {}).get('atr')}",
        ])
        for zone in (self.last_market.get("zones") or [])[:8]:
            report.append(f"  {zone.get('direction')} {zone.get('lower')}-{zone.get('upper')} raw_score={zone.get('quality_score')} touches={zone.get('touches')}")
        report.extend([
            "",
            "NEWS",
            f"Calendar: {news.get('provider_state')} | error: {news.get('error')}",
            f"Events: {len(news.get('events') or [])} | headlines: {len(news.get('headlines') or [])}",
            f"Headline error: {news.get('headline_error')} | news restriction: {(news.get('restriction') or {}).get('status')}",
            "",
            "NOTES",
            "- This script only monitors API data, it does not place trades or change settings.",
            "- No connection to ChatGPT or external notification service is established.",
            "- Audit counts start at the monitor's first audit fetch, not at account inception.",
            "- MT5 open-risk reporting and broker fills should be validated independently.",
            "- Check logs before sharing: they may include trading/account details, but no API key is requested.",
            "",
            "LOG LOCATION",
            str(self.output),
        ])
        path = self.output / "latest_report.txt"
        path.write_text("\n".join(report) + "\n", encoding="utf-8")

    def run(self) -> None:
        print("GoldSentinel Monitor — read-only", flush=True)
        print("API:", self.args.url, flush=True)
        print("Daily logs:", self.output, flush=True)
        print("Current summary:", self.output / "latest_report.txt", flush=True)
        print("Stop with Ctrl+C\n", flush=True)
        next_tick = time.monotonic()
        cycle = 0
        try:
            while self.running and (self.args.max_cycles == 0 or cycle < self.args.max_cycles):
                cycle += 1
                start = time.monotonic()
                try:
                    self.status_once()
                except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                    self.counts["status_failures"] += 1
                    self.last_error = safe_text(exc, 300)
                    self.transition("api_connection", "DOWN", "Local backend API", "ERROR")
                    self.transition("api_error", self.last_error, "API request error", "ERROR")
                current = time.monotonic()
                if current - self.last_logs_fetch >= self.args.logs_interval:
                    self.last_logs_fetch = current
                    try:
                        self.logs_once()
                    except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                        self.event("LOGS_ERROR", f"Cannot read backend audit logs: {safe_text(exc)}", "WARNING")
                if current - self.last_market_fetch >= self.args.market_interval:
                    self.last_market_fetch = current
                    try:
                        self.market_once()
                    except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                        self.event("MARKET_ERROR", f"Cannot inspect M15 zones/candles: {safe_text(exc)}", "WARNING")
                if current - self.last_report >= self.args.report_interval:
                    self.last_report = current
                    self.report()
                if not self.running:
                    break
                next_tick = max(next_tick + self.args.interval, start + self.args.interval)
                delay = max(0.0, next_tick - time.monotonic())
                if delay:
                    time.sleep(delay)
        except KeyboardInterrupt:
            pass
        finally:
            self.report()
            self.event("MONITOR_STOP", "Local monitor stopped")
            print("\nSaved report:", self.output / "latest_report.txt", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only live GoldSentinel API monitor (does not trade)")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="GoldSentinel backend URL")
    parser.add_argument("--output", default="monitoring", help="Output folder (default: ./monitoring)")
    parser.add_argument("--interval", type=float, default=10.0, help="Status sampling interval (seconds)")
    parser.add_argument("--market-interval", type=float, default=60.0, help="M15 market and zones sample interval (seconds)")
    parser.add_argument("--logs-interval", type=float, default=10.0, help="Backend audit log interval (seconds)")
    parser.add_argument("--report-interval", type=float, default=60.0, help="Latest report refresh interval (seconds)")
    parser.add_argument("--stale-seconds", type=float, default=90.0, help="Warn if quote timestamp is older than this; market closure is possible")
    parser.add_argument("--max-spread", type=float, default=1.0, help="Warn if gold bid/ask spread exceeds this dollar value")
    parser.add_argument("--contract-size", type=float, default=100.0, help="Contract size for independent approximate position stop-risk estimation")
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP request timeout (seconds)")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional: stop after N status cycles (0 means continuous)")
    args = parser.parse_args()
    if args.interval < 1 or args.market_interval < 1 or args.logs_interval < 1 or args.report_interval < 1:
        parser.error("Intervals must be at least one second")
    if args.timeout <= 0 or args.max_spread < 0 or args.contract_size <= 0 or args.max_cycles < 0:
        parser.error("Timeout, spread, contract size or cycle limit is invalid")
    monitor = Monitor(args)
    def _stop(_signum, _frame):
        monitor.running = False
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    monitor.run()


if __name__ == "__main__":
    main()
