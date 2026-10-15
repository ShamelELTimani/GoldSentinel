from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock

from .brokers.base import Broker
from .database import Repository
from .domain import (
    BrokerResult,
    DecisionAction,
    OrderRequest,
    RiskRequest,
    RuntimeSettings,
    Side,
    TradeCandidate,
)
from .risk import RiskManager


class ExecutionManager:
    def __init__(self, broker: Broker, repository: Repository) -> None:
        self.broker = broker
        self.repository = repository
        self.risk = RiskManager()
        self._lock = Lock()
        self._results: dict[str, BrokerResult] = {}
        self._uncertain: set[str] = set()

    def execute_candidate(
        self, candidate: TradeCandidate, runtime: RuntimeSettings, idempotency_key: str
    ) -> BrokerResult:
        with self._lock:
            if idempotency_key in self._results:
                return self._results[idempotency_key]
            if idempotency_key in self._uncertain:
                reconciled = self._reconcile(candidate, idempotency_key)
                if reconciled:
                    return reconciled
                return BrokerResult(success=False, state="UNCERTAIN", message="Submission is quarantined pending broker reconciliation")
            persisted = self._persistent_guard(candidate, idempotency_key)
            if persisted:
                return persisted
            rejection = self._safety_preflight(candidate, runtime)
            if rejection:
                if "expired" in rejection.lower():
                    self._save_lifecycle(candidate, "EXPIRED", rejection)
                return self._reject(candidate, rejection, persist=False)
            order, rejection = self._reprice_and_resize(candidate, runtime, idempotency_key)
            if rejection or order is None:
                return self._reject(candidate, rejection or "Trade revalidation failed")

            self._save_lifecycle(candidate, "SUBMITTING", "Order submission started", increment_attempt=True)

            self.repository.audit(
                "ORDER_SUBMITTING",
                "Order passed fresh-price, spread, stop, reward/risk, and exposure checks",
                {"candidate_id": candidate.id, "key": idempotency_key, "entry": order.entry},
            )
            try:
                result = self.broker.submit_market_order(candidate.symbol, order)
            except Exception as exc:
                self._uncertain.add(idempotency_key)
                result = BrokerResult(
                    success=False,
                    state="UNCERTAIN",
                    message=f"Broker response uncertain: {exc}",
                )
            if result.state == "UNCERTAIN":
                self._uncertain.add(idempotency_key)
                reconciled = self._reconcile(candidate, idempotency_key)
                if reconciled:
                    result = reconciled
                else:
                    self._save_lifecycle(candidate, "UNCERTAIN", result.message, payload=result.model_dump())
            elif result.success:
                self._results[idempotency_key] = result
                self._save_lifecycle(candidate, "FILLED", result.message, result.position_ticket, result.model_dump())
                self._record_trade(result, candidate)
            else:
                self._save_lifecycle(
                    candidate,
                    "REJECTED",
                    result.message,
                    payload=result.model_dump(),
                    retry_after=datetime.now(timezone.utc) + timedelta(seconds=120),
                )
            self.repository.audit(
                "ORDER_RESULT",
                result.message,
                result.model_dump(),
                "INFO" if result.success else "ERROR",
            )
            return result

    def _reject(self, candidate: TradeCandidate, message: str, persist: bool = True) -> BrokerResult:
        self.repository.audit(
            "ORDER_REJECTED", message, {"candidate_id": candidate.id}, "WARNING"
        )
        result = BrokerResult(success=False, state="REJECTED", message=message)
        if persist:
            self._save_lifecycle(candidate, "REJECTED", message, payload=result.model_dump(), retry_after=datetime.now(timezone.utc) + timedelta(seconds=120))
        return result

    def _persistent_guard(self, candidate: TradeCandidate, idempotency_key: str) -> BrokerResult | None:
        fingerprint = candidate.setup_fingerprint
        if not fingerprint or not hasattr(self.repository, "setup_lifecycle"):
            return None
        record = self.repository.setup_lifecycle(fingerprint)
        if not record:
            return None
        state = record["state"]
        if state == "FILLED":
            return BrokerResult(success=True, state="FILLED", message="Setup was already filled; duplicate submission suppressed", position_ticket=record.get("broker_ticket"))
        if state in {"SUBMITTING", "UNCERTAIN"}:
            reconciled = self._reconcile(candidate, idempotency_key)
            if reconciled:
                return reconciled
            return BrokerResult(success=False, state="UNCERTAIN", message="Setup has an unresolved broker submission; duplicate submission suppressed")
        retry_after = record.get("retry_after")
        if state == "REJECTED" and retry_after:
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < retry_after:
                return BrokerResult(success=False, state="REJECTED", message=f"Setup retry cooling down after rejection: {record.get('detail', '')}")
        return None

    def _save_lifecycle(
        self,
        candidate: TradeCandidate,
        state: str,
        detail: str,
        broker_ticket: str | None = None,
        payload: dict | None = None,
        retry_after: datetime | None = None,
        increment_attempt: bool = False,
    ) -> None:
        if candidate.setup_fingerprint and hasattr(self.repository, "save_setup_lifecycle"):
            self.repository.save_setup_lifecycle(
                candidate.setup_fingerprint,
                state,
                candidate.id,
                broker_ticket,
                detail,
                payload,
                retry_after,
                increment_attempt,
            )

    def _record_trade(self, result: BrokerResult, candidate: TradeCandidate) -> None:
        ticket = result.position_ticket or result.order_ticket
        if ticket and hasattr(self.repository, "record_trade_lifecycle"):
            self.repository.record_trade_lifecycle(ticket, candidate, candidate.entry)

    def _reconcile(self, candidate: TradeCandidate, idempotency_key: str) -> BrokerResult | None:
        try:
            marker = f"GS:{idempotency_key[:20]}"
            match = next(
                (
                    position
                    for position in self.broker.positions(candidate.symbol)
                    if self.broker.is_owned_position(position) and (position.comment == marker or position.setup_fingerprint == candidate.setup_fingerprint)
                ),
                None,
            )
        except Exception:
            return None
        if not match:
            return None
        result = BrokerResult(
            success=True,
            state="FILLED",
            message="Broker position reconciled after an uncertain submission",
            position_ticket=match.ticket,
            filled_volume=match.volume,
            raw={"reconciled": True},
        )
        self._uncertain.discard(idempotency_key)
        self._results[idempotency_key] = result
        self._save_lifecycle(candidate, "FILLED", result.message, match.ticket, result.model_dump())
        self._record_trade(result, candidate)
        return result

    def _safety_preflight(
        self, candidate: TradeCandidate, runtime: RuntimeSettings
    ) -> str | None:
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
        return None

    def _reprice_and_resize(
        self, candidate: TradeCandidate, runtime: RuntimeSettings, idempotency_key: str
    ) -> tuple[OrderRequest | None, str | None]:
        account = self.broker.account()
        if not account.is_demo:
            return None, "Actual MT5 account is not DEMO"
        quote = self.broker.quote(candidate.symbol)
        quote_age = max(0.0, (datetime.now(timezone.utc) - quote.timestamp).total_seconds())
        if quote_age > 60:
            return None, f"Executable quote is stale ({quote_age:.0f}s old)"

        spec = self.broker.symbol_spec(candidate.symbol)
        point = float(spec.get("point", 0.01))
        min_stop = max(float(spec.get("trade_stops_level", 0)), float(spec.get("trade_freeze_level", 0))) * point
        side = Side(candidate.action.value)
        price = quote.ask if side == Side.buy else quote.bid
        allowed_drift = candidate.max_price_deviation or max(quote.spread * 2, point * 20)
        if abs(price - candidate.entry) > allowed_drift:
            return None, (
                f"Executable price moved {abs(price - candidate.entry):.2f}, beyond the "
                f"allowed {allowed_drift:.2f}; fresh analysis is required"
            )

        spread_limit = max(candidate.spread_at_signal * 2.5, point * 50)
        if quote.spread > spread_limit:
            return None, f"Spread widened to {quote.spread:.2f}, above the {spread_limit:.2f} limit"

        stop_loss, take_profit = self.broker.normalize_protective_levels(
            candidate.symbol, side, quote, candidate.stop_loss, candidate.take_profit
        )
        trigger = quote.bid if side == Side.buy else quote.ask
        if side == Side.buy:
            stop_distance = trigger - stop_loss
            target_distance = take_profit - trigger
        else:
            stop_distance = stop_loss - trigger
            target_distance = trigger - take_profit
        if stop_distance <= 0 or target_distance <= 0:
            return None, "Fresh executable price invalidates stop-loss or take-profit direction"
        if min(stop_distance, target_distance) < min_stop:
            return None, "Fresh executable price violates the broker minimum stop distance"
        reward_risk = target_distance / stop_distance
        if reward_risk < runtime.risk.preferred_min_rr:
            return None, (
                f"Fresh executable reward-to-risk {reward_risk:.2f} is below "
                f"{runtime.risk.preferred_min_rr:.2f}"
            )

        positions = self.broker.positions(candidate.symbol)
        circuit = self.repository.risk_circuit_state(account, runtime.risk.cooldown_minutes) if hasattr(self.repository, "risk_circuit_state") else {}
        spec = {**spec, **circuit, "loss_per_lot": self.broker.loss_at_stop(candidate.symbol, side, 1.0, price, stop_loss)}
        risk = self.risk.calculate(
            RiskRequest(
                side=side,
                entry=price,
                stop_loss=stop_loss,
                quality_score=min(candidate.quality_score, 100),
            ),
            account,
            runtime.risk,
            runtime.strategy_profile,
            spec,
            positions,
            float(spec.get("daily_drawdown_pct", 0)),
            int(spec.get("consecutive_losses", 0)),
        )
        if not risk.allowed:
            return None, f"Fresh risk calculation rejected the order: {risk.reason}"
        if risk.risk_pct > runtime.risk.max_risk_per_trade_pct + 1e-9:
            return None, "Fresh order exceeds the current per-trade risk limit"

        candidate.entry = round(price, 5)
        candidate.stop_loss = stop_loss
        candidate.take_profit = take_profit
        candidate.volume = risk.volume
        candidate.risk_amount = risk.risk_amount
        candidate.risk_pct = risk.risk_pct
        candidate.reward_risk = round(reward_risk, 2)
        candidate.spread_at_signal = quote.spread
        return (
            OrderRequest(
                side=side,
                volume=risk.volume,
                entry=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                client_order_id=idempotency_key,
                candidate_id=candidate.id,
                max_price_deviation=allowed_drift,
            ),
            None,
        )
