from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock

from .backtest import BacktestConfig, run_backtest, walk_forward
from .brokers.base import Broker
from .brokers.mock import MockBroker
from .config import Settings
from .database import Repository
from .domain import AccountSnapshot, DecisionAction, DecisionClass, MarketContext, Position, Quote, RuntimeSettings, StrategyProfile, Timeframe, TradeCandidate, TradingMode
from .execution import ExecutionManager
from .historical_data import HistoricalDataPipeline
from .market import indicators, validate_candles
from .market_context import CONTEXT_FRAMES, MarketContextEngine
from .news import NewsEngine
from .position_manager import PositionManager
from .risk import RiskManager
from .setups import SetupEngine
from .strategy import StrategyEngine
from .zones import detect_zones, structure_summary


class GoldSentinelService:
    def __init__(self, settings: Settings, broker: Broker | None = None, repository: Repository | None = None) -> None:
        self.settings = settings
        self.repository = repository or Repository()
        self.runtime = self.repository.runtime_settings()
        self.broker = broker or self._make_broker()
        self.risk = RiskManager()
        self.news = NewsEngine(settings)
        self.strategy = StrategyEngine(self.risk)
        self.execution = ExecutionManager(self.broker, self.repository)
        self.context_engine = MarketContextEngine()
        self.setup_engine = SetupEngine()
        self.position_manager = PositionManager(self.broker, self.repository)
        self.history = HistoricalDataPipeline(self.broker, self.repository, settings.symbol, settings.history_batch_size)
        self.candidates: dict[str, TradeCandidate] = {}
        self.latest_candidate: TradeCandidate | None = None
        self.market_qualities: dict[Timeframe, dict] = {}
        self.last_quote: Quote | None = None
        self.last_account: AccountSnapshot | None = None
        self.last_positions: list[Position] = []
        self.latest_context: MarketContext | None = None
        self.detected_setups = []
        self.management_decisions = []
        self.context_qualities: dict[str, dict] = {}
        self._context_refreshed_at: datetime | None = None
        self._last_signal_signature: str | None = None
        self._last_signal_audit_at: datetime | None = None
        self._lock = RLock()

    def _make_broker(self) -> Broker:
        if self.settings.broker.lower() == "mt5":
            from .brokers.mt5 import MT5Broker
            return MT5Broker()
        if self.settings.broker.lower() == "mock":
            return MockBroker()
        raise ValueError(f"Unsupported broker {self.settings.broker!r}; use 'mt5' or 'mock'. GoldSentinel will not silently fall back to mock data")

    def start(self) -> None:
        connected = self.broker.connect()
        self.repository.audit("SYSTEM", "GoldSentinel started disarmed", {"broker": self.settings.broker, "connected": connected})

    def stop(self) -> None:
        self.broker.disconnect()

    def update_runtime(self, updated: RuntimeSettings) -> RuntimeSettings:
        with self._lock:
            # Emergency state has a dedicated reset endpoint and cannot be cleared
            # incidentally by a stale settings form.
            emergency_stop = self.runtime.emergency_stop
            # Any settings or mode mutation requires a fresh explicit arm action.
            self.runtime = updated.model_copy(update={"armed": False, "emergency_stop": emergency_stop})
            self.repository.save_runtime_settings(self.runtime)
            self.repository.audit("CONFIG", "Runtime settings updated", self.runtime.model_dump(exclude={"armed"}))
            return self.runtime

    def set_paused(self, paused: bool) -> RuntimeSettings:
        with self._lock:
            self.runtime.paused = paused
            if paused:
                self.runtime.armed = False
            self.repository.save_runtime_settings(self.runtime)
            self.repository.audit("SAFETY", "Trading paused and automatic execution disarmed" if paused else "Trading resumed; automatic execution remains disarmed")
            return self.runtime

    def set_armed(self, armed: bool) -> RuntimeSettings:
        with self._lock:
            if armed:
                if self.runtime.emergency_stop:
                    raise ValueError("Clear the emergency stop before arming")
                if self.runtime.paused:
                    raise ValueError("Trading is paused; press Resume before arming")
                if self.runtime.mode != TradingMode.full_auto:
                    raise ValueError("Automatic execution can only be armed in FULL_AUTO mode")
                self.market_context(force=True)
                quote = self.broker.quote(self.settings.symbol)
                account = self.broker.account()
                positions = self.broker.positions(self.settings.symbol)
                readiness = self._readiness(quote, account, positions)
                blockers = [item["detail"] for item in readiness["checks"] if item["blocking"] and item["id"] != "armed"]
                if blockers:
                    raise ValueError("Automatic trading is not ready: " + "; ".join(blockers))
            self.runtime.armed = armed
            self.repository.audit("SAFETY", "Automatic trading armed" if armed else "Automatic trading disarmed")
            return self.runtime

    def emergency_stop(self) -> dict:
        with self._lock:
            self.runtime.emergency_stop = True
            self.runtime.armed = False
            results = [self.broker.cancel_pending(str(order.get("ticket", order.get("order", "")))) for order in self.broker.pending_orders()]
            self.repository.save_runtime_settings(self.runtime)
            self.repository.audit("SAFETY", "Emergency stop activated; new orders blocked", {"cancellations": [r.model_dump() for r in results]}, "WARNING")
            return {"runtime": self.runtime, "pending_cancellations": results, "positions_unchanged": True}

    def reset_emergency_stop(self) -> RuntimeSettings:
        self.runtime.emergency_stop = False
        self.runtime.armed = False
        self.repository.save_runtime_settings(self.runtime)
        self.repository.audit("SAFETY", "Emergency stop reset; system remains disarmed")
        return self.runtime

    def market_snapshot(self, timeframe: Timeframe = Timeframe.m15, count: int = 350, offset: int = 0) -> dict:
        candles = self.broker.candles(self.settings.symbol, timeframe, count, offset)
        quality = validate_candles(candles, timeframe)
        self.market_qualities[timeframe] = quality.__dict__
        metrics = indicators(candles) if len(candles) >= 20 else {"ema20": [], "ema50": [], "rsi": [], "atr": [], "latest": {}}
        zones = detect_zones(candles, timeframe)
        structure = structure_summary(candles)
        return {"symbol": self.settings.symbol, "timeframe": timeframe, "source": self._quote().source, "candles": candles, "quality": quality.__dict__, "indicators": metrics, "zones": zones, "structure": structure}

    def _quote(self) -> Quote:
        quote = self.broker.quote(self.settings.symbol)
        self.last_quote = quote
        return quote

    def quote_snapshot(self) -> dict:
        try:
            quote = self._quote()
            error = None
            connected = self.broker.is_connected()
        except Exception as exc:
            quote = self.last_quote
            error = str(exc)
            connected = False
        if quote is None:
            return {"connected": False, "quote": None, "spread": None, "age_seconds": None, "state": "DISCONNECTED", "stale": True, "error": error or "No broker quote has been received"}
        age = max(0.0, (datetime.now(timezone.utc) - quote.timestamp).total_seconds())
        live_window = max(5.0, self.settings.polling_seconds * 3)
        state = "LIVE" if connected and age <= live_window else "QUIET" if connected and age <= 60 else "STALE" if connected else "DISCONNECTED"
        return {"connected": connected, "quote": quote, "spread": quote.spread, "age_seconds": round(age, 3), "state": state, "stale": state in {"STALE", "DISCONNECTED"}, "error": error}

    def live_market(self, timeframe: Timeframe) -> dict:
        candles = self.broker.candles(self.settings.symbol, timeframe, 2)
        return {"symbol": self.settings.symbol, "timeframe": timeframe, "candles": candles, **self.quote_snapshot()}

    def market_context(self, force: bool = False) -> MarketContext:
        now = datetime.now(timezone.utc)
        if (
            not force
            and self.latest_context is not None
            and self._context_refreshed_at is not None
            and (now - self._context_refreshed_at).total_seconds() < self.settings.analysis_refresh_seconds
        ):
            return self.latest_context
        candles_by_timeframe = {}
        errors = []
        for frame in CONTEXT_FRAMES:
            candles = self.broker.candles(self.settings.symbol, frame, 260)
            quality = validate_candles(candles, frame)
            self.market_qualities[frame] = quality.__dict__
            self.context_qualities[frame.value] = quality.__dict__
            if not quality.valid:
                errors.extend(f"{frame.value}: {item}" for item in (quality.errors or ["market data invalid"]))
            candles_by_timeframe[frame] = candles
        if errors:
            raise ValueError("; ".join(errors))
        self.latest_context = self.context_engine.build(self.settings.symbol, candles_by_timeframe)
        self._context_refreshed_at = now
        return self.latest_context

    def analyze(self, timeframe: Timeframe = Timeframe.m15) -> TradeCandidate:
        quote = self._quote()
        try:
            context = self.market_context()
            zone_candles = self.broker.candles(self.settings.symbol, Timeframe.m15, 350)
            zones = detect_zones(zone_candles, Timeframe.m15)
            self.detected_setups = self.setup_engine.detect(context, quote, zones, self.runtime.strategy_profile)
            if not self.detected_setups:
                candidate = self.strategy._no_trade(
                    self.settings.symbol,
                    Timeframe.m5,
                    (quote.bid + quote.ask) / 2,
                    [*context.reasons, "No candle-confirmed setup met the active profile threshold"],
                    DecisionClass.market,
                )
            else:
                account = self.broker.account()
                positions = self.broker.positions(self.settings.symbol)
                spec = self.broker.symbol_spec(self.settings.symbol)
                circuit = self.repository.risk_circuit_state(account, self.runtime.risk.cooldown_minutes) if hasattr(self.repository, "risk_circuit_state") else {}
                news = self.news.risk_context(policy=self.runtime.risk.news_policy)
                candidates = []
                for setup in self.detected_setups:
                    setup_spec = {**spec, **circuit, "loss_per_lot": self.broker.loss_at_stop(self.settings.symbol, setup.side, 1.0, quote.ask if setup.side.value == "BUY" else quote.bid, setup.stop_loss)}
                    evaluated = self.strategy.evaluate_setup(
                        self.settings.symbol, setup, quote, account, positions, self.runtime.risk,
                        self.runtime.strategy_profile, setup_spec, news, context.model_dump(mode="json"),
                    )
                    if evaluated.action == DecisionAction.no_trade:
                        evaluated.setup_type = setup.setup_type
                        evaluated.setup_fingerprint = setup.fingerprint
                        evaluated.entry_trigger = setup.trigger
                        evaluated.in_zone = setup.in_zone
                        evaluated.timeframe_context = context.model_dump(mode="json")
                    candidates.append(evaluated)
                    self._record_setup_detection(evaluated)
                candidate = next((item for item in candidates if item.action != DecisionAction.no_trade), candidates[0])
        except Exception as exc:
            reason = f"Multi-timeframe market context unavailable: {exc}"
            candidate = self.strategy._no_trade(self.settings.symbol, timeframe, (quote.bid + quote.ask) / 2, [reason], DecisionClass.operational, [reason])
        self.candidates[candidate.id] = candidate
        self.latest_candidate = candidate
        self._audit_signal(candidate)
        if candidate.action != DecisionAction.no_trade:
            quote = self.broker.quote(self.settings.symbol)
            account = self.broker.account()
            positions = self.broker.positions(self.settings.symbol)
            readiness = self._readiness(quote, account, positions)
            active = [item["detail"] for item in readiness["checks"] if item["blocking"]]
            candidate.blockers = list(dict.fromkeys(candidate.blockers + active))
            if self.runtime.mode == TradingMode.full_auto and readiness["ready"]:
                result = self.execution.execute_candidate(candidate, self.runtime, f"auto:{candidate.setup_fingerprint or candidate.zone_id or candidate.id}")
                candidate.status = "EXECUTED" if result.success else result.state
            elif self.runtime.mode == TradingMode.full_auto:
                candidate.status = "BLOCKED_OPERATIONAL"
        return candidate

    def _record_setup_detection(self, candidate: TradeCandidate) -> None:
        if not candidate.setup_fingerprint or not hasattr(self.repository, "setup_lifecycle"):
            return
        existing = self.repository.setup_lifecycle(candidate.setup_fingerprint)
        if existing and existing["state"] in {"SUBMITTING", "FILLED", "UNCERTAIN"}:
            return
        state = "QUALIFIED" if candidate.action != DecisionAction.no_trade else "DETECTED"
        self.repository.save_setup_lifecycle(candidate.setup_fingerprint, state, candidate.id, detail=candidate.status, payload={"setup_type": candidate.setup_type.value if candidate.setup_type else None})

    def _audit_signal(self, candidate: TradeCandidate) -> None:
        signature = "|".join([candidate.action.value, candidate.decision_class.value, candidate.setup_fingerprint or "", candidate.status, *candidate.blockers])
        now = datetime.now(timezone.utc)
        if signature != self._last_signal_signature or not self._last_signal_audit_at or now - self._last_signal_audit_at >= timedelta(minutes=1):
            self.repository.audit("SIGNAL", candidate.action.value, candidate.model_dump())
            self._last_signal_signature = signature
            self._last_signal_audit_at = now

    def manage_positions(self) -> list:
        try:
            context = self.market_context()
            zone_candles = self.broker.candles(self.settings.symbol, Timeframe.m15, 250)
            zones = detect_zones(zone_candles, Timeframe.m15)
            news = self.news.risk_context(policy=self.runtime.risk.news_policy)
            self.management_decisions = self.position_manager.manage(self.settings.symbol, self.runtime, context, zones, news)
        except Exception as exc:
            self.repository.audit("POSITION_MANAGEMENT", "Management cycle unavailable", {"error": str(exc)}, "WARNING")
            self.management_decisions = []
        return self.management_decisions

    def refresh_history(self) -> dict:
        plan = {
            Timeframe.m1: self.settings.history_m1_days,
            Timeframe.m5: self.settings.history_m5_days,
            Timeframe.m15: self.settings.history_m15_days,
            Timeframe.h1: self.settings.history_h1_days,
            Timeframe.h4: self.settings.history_h4_days,
        }
        return self.history.refresh_core(plan)

    def market_diagnostics(self, count: int = 350) -> dict:
        results = {}
        for timeframe in Timeframe:
            try:
                candles = self.broker.candles(self.settings.symbol, timeframe, count)
                quality = validate_candles(candles, timeframe)
                self.market_qualities[timeframe] = quality.__dict__
                results[timeframe.value] = {"loaded": True, "count": len(candles), "complete_count": sum(c.complete for c in candles), "last_timestamp": candles[-1].timestamp if candles else None, "last_complete": candles[-1].complete if candles else None, "quality": quality.__dict__}
            except Exception as exc:
                results[timeframe.value] = {"loaded": False, "count": 0, "quality": {"valid": False, "stale": True, "errors": [str(exc)], "warnings": [], "gaps": [], "blocking_gaps": [], "expected_gaps": []}}
        valid = sum(1 for item in results.values() if item["quality"]["valid"])
        return {"symbol": self.settings.symbol, "source": self.broker.quote(self.settings.symbol).source, "checked_at": datetime.now(timezone.utc), "valid_timeframes": valid, "total_timeframes": len(Timeframe), "all_valid": valid == len(Timeframe), "timeframes": results}

    def approve(self, candidate_id: str, idempotency_key: str) -> dict:
        candidate = self.candidates.get(candidate_id)
        if not candidate:
            raise KeyError("Candidate not found")
        if self.runtime.mode != TradingMode.confirmation:
            raise ValueError("Approval is only valid in CONFIRMATION mode")
        # Rebuild analysis immediately and require same side before sending.
        fresh = self.analyze(candidate.timeframe)
        if fresh.action != candidate.action or fresh.action == DecisionAction.no_trade:
            candidate.status = "REVALIDATION_FAILED"
            return {"candidate": candidate, "result": {"success": False, "state": "REJECTED", "message": "Fresh analysis no longer supports the original direction"}}
        result = self.execution.execute_candidate(fresh, self.runtime, idempotency_key)
        fresh.status = "EXECUTED" if result.success else result.state
        return {"candidate": fresh, "result": result}

    def status(self) -> dict:
        quote_health = self.quote_snapshot()
        quote = quote_health["quote"] or Quote(symbol=self.settings.symbol, bid=0, ask=0, timestamp=datetime.now(timezone.utc), source="UNAVAILABLE")
        try:
            account = self.broker.account()
            positions = self.broker.positions(self.settings.symbol)
            pending_orders = self.broker.pending_orders(self.settings.symbol)
            self.last_account = account
            self.last_positions = positions
        except Exception as exc:
            account = self.last_account or AccountSnapshot(
                balance=0,
                equity=0,
                free_margin=0,
                margin=0,
                leverage=0,
                is_demo=False,
            )
            positions = self.last_positions
            pending_orders = []
            quote_health.update(
                {
                    "connected": False,
                    "state": "DISCONNECTED",
                    "stale": True,
                    "error": quote_health.get("error") or str(exc),
                }
            )
        open_risk = sum(item.initial_risk for item in positions)
        readiness = self._readiness(quote, account, positions)
        return {"connected": quote_health["connected"], "broker": self.settings.broker.upper(), "symbol": self.settings.symbol, "data_source": quote.source, "quote": quote, "quote_health": {key: value for key, value in quote_health.items() if key != "quote"}, "spread": quote.spread, "account": account, "runtime": self.runtime, "positions": positions, "pending_orders": pending_orders, "open_risk": round(open_risk, 2), "remaining_risk": round(max(0, account.equity * self.runtime.risk.max_combined_risk_pct / 100 - open_risk), 2), "latest_candidate": self.latest_candidate, "market_context": self.latest_context, "detected_setups": self.detected_setups, "position_management": self.management_decisions, "readiness": readiness, "server_time": datetime.now(timezone.utc), "news": self.news.status(self.runtime.risk.news_policy)}

    def _readiness(self, quote, account, positions) -> dict:
        context_ready = all(self.market_qualities.get(frame, {}).get("valid") for frame in CONTEXT_FRAMES)
        news = self.news.risk_context(policy=self.runtime.risk.news_policy)
        combined_limit = account.equity * self.runtime.risk.max_combined_risk_pct / 100
        open_risk = sum(max(position.initial_risk, 0) for position in positions)
        broker_expected = "MT5" if self.settings.broker.lower() == "mt5" else "MOCK"
        try:
            broker_connected = self.broker.is_connected()
        except Exception:
            broker_connected = False
        checks = [
            {"id": "mt5_connection", "label": "MT5 connection", "ready": broker_connected and account.is_demo and quote.source == broker_expected, "detail": f"Connected to {quote.source} demo account" if broker_connected and account.is_demo and quote.source == broker_expected else "MT5 demo connection or broker data source is not verified"},
            {"id": "market_data", "label": "Market data", "ready": context_ready, "detail": "H4/H1/M15/M5/M1 candles are fresh and structurally valid" if context_ready else "Required multi-timeframe market data has not validated or has a recent blocking issue"},
            {"id": "news", "label": "News availability", "ready": not news["block"], "detail": news["reason"]},
            {"id": "risk", "label": "Risk limits", "ready": len(positions) < self.runtime.risk.max_positions and open_risk < combined_limit, "detail": "Position count and combined open risk are within limits" if len(positions) < self.runtime.risk.max_positions and open_risk < combined_limit else "Position count or combined open-risk limit is reached"},
            {"id": "mode", "label": "Execution mode", "ready": self.runtime.mode == TradingMode.full_auto, "detail": "FULL_AUTO mode selected" if self.runtime.mode == TradingMode.full_auto else f"{self.runtime.mode.value} mode does not submit automatic orders"},
            {"id": "pause", "label": "Pause state", "ready": not self.runtime.paused, "detail": "Trading loop is running" if not self.runtime.paused else "Trading is paused; press Resume to continue analysis/execution"},
            {"id": "emergency", "label": "Emergency stop", "ready": not self.runtime.emergency_stop, "detail": "Emergency stop is clear" if not self.runtime.emergency_stop else "Emergency stop is active"},
            {"id": "armed", "label": "Armed status", "ready": self.runtime.armed, "detail": "Automatic order submission is explicitly armed" if self.runtime.armed else "Automatic order submission is disarmed"},
        ]
        for item in checks:
            item["blocking"] = not item["ready"]
        blockers = [item["detail"] for item in checks if item["blocking"]]
        return {"ready": not blockers, "can_arm": not any(item["blocking"] for item in checks if item["id"] != "armed"), "checks": checks, "blockers": blockers}

    def backtest(self, timeframe: Timeframe, profile: StrategyProfile, count: int = 2500, walk_forward_mode: bool = False) -> dict:
        candles = self.broker.candles(self.settings.symbol, timeframe, count)
        if walk_forward_mode:
            return walk_forward(candles, timeframe, profile)
        return run_backtest(candles, timeframe, profile, BacktestConfig(max_risk_pct=self.runtime.risk.max_risk_per_trade_pct))

