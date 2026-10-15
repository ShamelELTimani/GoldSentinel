from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

from .brokers.base import Broker
from .database import Repository
from .domain import BrokerResult, DecisionAction, OrderRequest, RuntimeSettings, Side, TradeCandidate


class ExecutionManager:
    def __init__(self, broker: Broker, repository: Repository) -> None:
        self.broker = broker
        self.repository = repository
        self._lock = Lock()
        self._results: dict[str, BrokerResult] = {}
        self._uncertain: set[str] = set()

    def execute_candidate(self, candidate: TradeCandidate, runtime: RuntimeSettings, idempotency_key: str) -> BrokerResult:
        with self._lock:
            if idempotency_key in self._results:
                return self._results[idempotency_key]
            if idempotency_key in self._uncertain:
                return BrokerResult(success=False, state="UNCERTAIN", message="Submission is quarantined pending broker reconciliation")
            rejection = self._preflight(candidate, runtime)
            if rejection:
                self.repository.audit("ORDER_REJECTED", rejection, {"candidate_id": candidate.id}, "WARNING")
                return BrokerResult(success=False, state="REJECTED", message=rejection)
            self.repository.audit("ORDER_SUBMITTING", "Order passed application preflight", {"candidate_id": candidate.id, "key": idempotency_key})
            order = OrderRequest(side=Side(candidate.action.value), volume=candidate.volume, entry=candidate.entry, stop_loss=candidate.stop_loss, take_profit=candidate.take_profit, client_order_id=idempotency_key, candidate_id=candidate.id)
            try:
                result = self.broker.submit_market_order(candidate.symbol, order)
            except Exception as exc:
                self._uncertain.add(idempotency_key)
                result = BrokerResult(success=False, state="UNCERTAIN", message=f"Broker response uncertain: {exc}")
            if result.state == "UNCERTAIN":
                self._uncertain.add(idempotency_key)
            else:
                self._results[idempotency_key] = result
            self.repository.audit("ORDER_RESULT", result.message, result.model_dump(), "INFO" if result.success else "ERROR")
            return result

    def _preflight(self, candidate: TradeCandidate, runtime: RuntimeSettings) -> str | None:
        if runtime.emergency_stop:
            return "Emergency stop is active"
        if runtime.paused:
            return "Trading is paused"
        if candidate.action == DecisionAction.no_trade:
            return "NO_TRADE decisions cannot be executed"
        if datetime.now(timezone.utc) >= candidate.expires_at:
            return "Candidate expired and must be revalidated"
        if not self.broker.is_connected():
            return "Broker is disconnected"
        account = self.broker.account()
        if not account.is_demo:
            return "Actual MT5 account is not DEMO"
        if candidate.risk_pct > runtime.risk.max_risk_per_trade_pct:
            return "Candidate exceeds current per-trade risk limit"
        return None

