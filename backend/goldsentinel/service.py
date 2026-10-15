from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from .backtest import BacktestConfig, run_backtest, walk_forward
from .brokers.base import Broker
from .brokers.mock import MockBroker
from .config import Settings
from .database import Repository
from .domain import DecisionAction, DecisionClass, RuntimeSettings, StrategyProfile, Timeframe, TradeCandidate, TradingMode
from .execution import ExecutionManager
from .market import indicators, validate_candles
from .news import NewsEngine
from .risk import RiskManager
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
        self.candidates: dict[str, TradeCandidate] = {}
        self.latest_candidate: TradeCandidate | None = None
        self.market_qualities: dict[Timeframe, dict] = {}
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
                self.market_snapshot(Timeframe.m15)
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
        return {"symbol": self.settings.symbol, "timeframe": timeframe, "source": self.broker.quote(self.settings.symbol).source, "candles": candles, "quality": quality.__dict__, "indicators": metrics, "zones": zones, "structure": structure}

    def analyze(self, timeframe: Timeframe = Timeframe.m15) -> TradeCandidate:
        snapshot = self.market_snapshot(timeframe)
        if not snapshot["quality"]["valid"]:
            reasons = snapshot["quality"]["errors"] or ["Recent unexpected market-data gap detected"]
            candidate = self.strategy._no_trade(self.settings.symbol, timeframe, snapshot["candles"][-1].close, reasons, DecisionClass.operational, reasons)
        else:
            news = self.news.risk_context(policy=self.runtime.risk.news_policy)
            candidate = self.strategy.evaluate(self.settings.symbol, timeframe, snapshot["candles"], snapshot["zones"], self.broker.account(), self.broker.positions(self.settings.symbol), self.runtime.risk, self.runtime.strategy_profile, self.broker.symbol_spec(self.settings.symbol), news)
        self.candidates[candidate.id] = candidate
        self.latest_candidate = candidate
        self.repository.audit("SIGNAL", candidate.action.value, candidate.model_dump())
        if candidate.action != DecisionAction.no_trade:
            quote = self.broker.quote(self.settings.symbol)
            account = self.broker.account()
            positions = self.broker.positions(self.settings.symbol)
            readiness = self._readiness(quote, account, positions)
            active = [item["detail"] for item in readiness["checks"] if item["blocking"]]
            candidate.blockers = list(dict.fromkeys(candidate.blockers + active))
            if self.runtime.mode == TradingMode.full_auto and readiness["ready"]:
                result = self.execution.execute_candidate(candidate, self.runtime, f"auto:{candidate.zone_id or candidate.id}")
                candidate.status = "EXECUTED" if result.success else result.state
            elif self.runtime.mode == TradingMode.full_auto:
                candidate.status = "BLOCKED_OPERATIONAL"
        return candidate

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
        quote = self.broker.quote(self.settings.symbol)
        account = self.broker.account()
        positions = self.broker.positions(self.settings.symbol)
        open_risk = sum(item.initial_risk for item in positions)
        readiness = self._readiness(quote, account, positions)
        return {"connected": self.broker.is_connected(), "broker": self.settings.broker.upper(), "symbol": self.settings.symbol, "data_source": quote.source, "quote": quote, "spread": quote.spread, "account": account, "runtime": self.runtime, "positions": positions, "pending_orders": self.broker.pending_orders(self.settings.symbol), "open_risk": round(open_risk, 2), "remaining_risk": round(max(0, account.equity * self.runtime.risk.max_combined_risk_pct / 100 - open_risk), 2), "latest_candidate": self.latest_candidate, "readiness": readiness, "server_time": datetime.now(timezone.utc), "news": self.news.status(self.runtime.risk.news_policy)}

    def _readiness(self, quote, account, positions) -> dict:
        quality = self.market_qualities.get(Timeframe.m15)
        news = self.news.risk_context(policy=self.runtime.risk.news_policy)
        combined_limit = account.equity * self.runtime.risk.max_combined_risk_pct / 100
        open_risk = sum(max(position.initial_risk, 0) for position in positions)
        broker_expected = "MT5" if self.settings.broker.lower() == "mt5" else "MOCK"
        checks = [
            {"id": "mt5_connection", "label": "MT5 connection", "ready": self.broker.is_connected() and account.is_demo and quote.source == broker_expected, "detail": f"Connected to {quote.source} demo account" if self.broker.is_connected() and account.is_demo and quote.source == broker_expected else "MT5 demo connection or broker data source is not verified"},
            {"id": "market_data", "label": "Market data", "ready": bool(quality and quality["valid"]), "detail": "M15 candles are fresh and structurally valid" if quality and quality["valid"] else "M15 market data has not validated or has a recent blocking issue"},
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

