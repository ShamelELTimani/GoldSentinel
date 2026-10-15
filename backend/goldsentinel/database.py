from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from typing import Iterator

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings
from .domain import Candle, RuntimeSettings, Timeframe, utcnow


class Base(DeclarativeBase):
    pass


class KeyValue(Base):
    __tablename__ = "key_value"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String, index=True)
    level: Mapped[str] = mapped_column(String, default="INFO")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="{}")


class TradeRecord(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket: Mapped[str] = mapped_column(String, unique=True, index=True)
    source: Mapped[str] = mapped_column(String, default="DEMO", index=True)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    setup: Mapped[str] = mapped_column(String, default="zone")
    timeframe: Mapped[str] = mapped_column(String, default="M15")
    session: Mapped[str] = mapped_column(String, default="OTHER")
    volume: Mapped[float] = mapped_column(Float)
    entry: Mapped[float] = mapped_column(Float)
    exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0)
    commission: Mapped[float] = mapped_column(Float, default=0)
    swap: Mapped[float] = mapped_column(Float, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    planned_rr: Mapped[float] = mapped_column(Float, default=0)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl + self.commission + self.swap


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String, default="DEMO")


class HistoricalCandle(Base):
    __tablename__ = "historical_candles"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "timestamp"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    timeframe: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    spread: Mapped[float] = mapped_column(Float, default=0)
    complete: Mapped[bool] = mapped_column(Boolean, default=True)


class SetupLifecycleRecord(Base):
    __tablename__ = "setup_lifecycle"
    fingerprint: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    candidate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    broker_ticket: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TradeLifecycleRecord(Base):
    __tablename__ = "trade_lifecycle"
    ticket: Mapped[str] = mapped_column(String, primary_key=True)
    setup_fingerprint: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    setup_type: Mapped[str] = mapped_column(String, default="UNKNOWN")
    entry_trigger: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[str] = mapped_column(Text, default="{}")
    initial_entry: Mapped[float] = mapped_column(Float)
    initial_stop: Mapped[float] = mapped_column(Float)
    initial_target: Mapped[float] = mapped_column(Float)
    initial_risk: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    management_log: Mapped[str] = mapped_column(Text, default="[]")


class TradeOutcomeRecord(Base):
    __tablename__ = "trade_outcomes"
    ticket: Mapped[str] = mapped_column(String, primary_key=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_r: Mapped[float] = mapped_column(Float, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    exit_reason: Mapped[str] = mapped_column(Text)
    broker_payload: Mapped[str] = mapped_column(Text, default="{}")


settings = get_settings()
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class Repository:
    def runtime_settings(self) -> RuntimeSettings:
        with session_scope() as db:
            row = db.get(KeyValue, "runtime_settings")
            if not row:
                return RuntimeSettings()
            saved = RuntimeSettings.model_validate_json(row.value)
            # Arming is intentionally volatile and never restored after restart.
            return saved.model_copy(update={"armed": False})

    def save_runtime_settings(self, value: RuntimeSettings) -> None:
        safe_value = value.model_copy(update={"armed": False})
        with session_scope() as db:
            row = db.get(KeyValue, "runtime_settings") or KeyValue(key="runtime_settings", value="")
            row.value = safe_value.model_dump_json()
            row.updated_at = utcnow()
            db.add(row)

    def audit(self, category: str, message: str, payload: dict | None = None, level: str = "INFO") -> None:
        with session_scope() as db:
            db.add(AuditEvent(category=category, message=message, payload=json.dumps(payload or {}, default=str), level=level))

    def audit_events(self, limit: int = 200) -> list[dict]:
        with session_scope() as db:
            rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)).all()
            return [{"id": r.id, "timestamp": r.timestamp, "category": r.category, "level": r.level, "message": r.message, "payload": json.loads(r.payload)} for r in rows]

    def closed_trades(self, start: datetime | None = None, end: datetime | None = None, source: str | None = None) -> list[TradeRecord]:
        with session_scope() as db:
            stmt = select(TradeRecord).where(TradeRecord.closed_at.is_not(None))
            if start:
                stmt = stmt.where(TradeRecord.closed_at >= start)
            if end:
                stmt = stmt.where(TradeRecord.closed_at <= end)
            if source:
                stmt = stmt.where(TradeRecord.source == source)
            return list(db.scalars(stmt.order_by(TradeRecord.closed_at)).all())

    def risk_circuit_state(self, account, cooldown_minutes: int = 60) -> dict:
        today = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
        trades = self.closed_trades(start=today)
        daily_net = sum(item.net_pnl for item in trades)
        baseline = max(account.balance - daily_net, account.equity, 1)
        daily_drawdown_pct = max(0.0, (baseline - account.equity) / baseline * 100)
        consecutive_losses = 0
        for trade in reversed(trades):
            if trade.net_pnl < 0:
                consecutive_losses += 1
            else:
                break
        last_closed = trades[-1].closed_at if trades else None
        if last_closed:
            if last_closed.tzinfo is None:
                last_closed = last_closed.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_closed >= timedelta(minutes=cooldown_minutes):
                consecutive_losses = 0
        return {"daily_drawdown_pct": daily_drawdown_pct, "consecutive_losses": consecutive_losses}

    def equity(self, start: datetime | None = None, source: str | None = None) -> list[EquitySnapshot]:
        with session_scope() as db:
            stmt = select(EquitySnapshot)
            if start:
                stmt = stmt.where(EquitySnapshot.timestamp >= start)
            if source:
                stmt = stmt.where(EquitySnapshot.source == source)
            return list(db.scalars(stmt.order_by(EquitySnapshot.timestamp)).all())

    def upsert_candles(self, symbol: str, timeframe: Timeframe, candles: list[Candle]) -> int:
        changed = 0
        with session_scope() as db:
            for candle in candles:
                row = db.scalar(select(HistoricalCandle).where(HistoricalCandle.symbol == symbol, HistoricalCandle.timeframe == timeframe.value, HistoricalCandle.timestamp == candle.timestamp))
                values = candle.model_dump()
                if row is None:
                    row = HistoricalCandle(symbol=symbol, timeframe=timeframe.value, **values)
                    db.add(row)
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                changed += 1
        return changed

    def historical_candles(self, symbol: str, timeframe: Timeframe, limit: int | None = None) -> list[Candle]:
        with session_scope() as db:
            stmt = select(HistoricalCandle).where(HistoricalCandle.symbol == symbol, HistoricalCandle.timeframe == timeframe.value).order_by(HistoricalCandle.timestamp.desc())
            if limit:
                stmt = stmt.limit(limit)
            rows = list(reversed(db.scalars(stmt).all()))
            return [Candle(timestamp=row.timestamp, open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume, spread=row.spread, complete=row.complete) for row in rows]

    def setup_lifecycle(self, fingerprint: str) -> dict | None:
        with session_scope() as db:
            row = db.get(SetupLifecycleRecord, fingerprint)
            if not row:
                return None
            return {"fingerprint": row.fingerprint, "state": row.state, "attempts": row.attempts, "candidate_id": row.candidate_id, "broker_ticket": row.broker_ticket, "detail": row.detail, "payload": json.loads(row.payload), "updated_at": row.updated_at, "retry_after": row.retry_after}

    def save_setup_lifecycle(self, fingerprint: str, state: str, candidate_id: str | None = None, broker_ticket: str | None = None, detail: str = "", payload: dict | None = None, retry_after: datetime | None = None, increment_attempt: bool = False) -> None:
        with session_scope() as db:
            row = db.get(SetupLifecycleRecord, fingerprint) or SetupLifecycleRecord(fingerprint=fingerprint, state=state)
            row.state = state
            row.candidate_id = candidate_id or row.candidate_id
            row.broker_ticket = broker_ticket or row.broker_ticket
            row.detail = detail
            row.payload = json.dumps(payload or {}, default=str)
            row.updated_at = utcnow()
            row.retry_after = retry_after
            if increment_attempt:
                row.attempts = (row.attempts or 0) + 1
            db.add(row)

    def record_trade_lifecycle(self, ticket: str, candidate, actual_entry: float | None = None) -> None:
        with session_scope() as db:
            row = db.get(TradeLifecycleRecord, ticket)
            if row:
                return
            db.add(TradeLifecycleRecord(ticket=ticket, setup_fingerprint=candidate.setup_fingerprint, setup_type=candidate.setup_type.value if candidate.setup_type else "ZONE", entry_trigger=candidate.entry_trigger or "", context=json.dumps(candidate.timeframe_context, default=str), initial_entry=actual_entry or candidate.entry, initial_stop=candidate.stop_loss, initial_target=candidate.take_profit, initial_risk=candidate.risk_amount, volume=candidate.volume, opened_at=utcnow()))

    def trade_lifecycle(self, ticket: str) -> dict | None:
        with session_scope() as db:
            row = db.get(TradeLifecycleRecord, ticket)
            if not row:
                return None
            return {"ticket": row.ticket, "setup_fingerprint": row.setup_fingerprint, "setup_type": row.setup_type, "entry_trigger": row.entry_trigger, "context": json.loads(row.context), "initial_entry": row.initial_entry, "initial_stop": row.initial_stop, "initial_target": row.initial_target, "initial_risk": row.initial_risk, "volume": row.volume, "opened_at": row.opened_at, "management_log": json.loads(row.management_log)}

    def record_management_action(self, ticket: str, action: str, reason: str, result: dict) -> None:
        with session_scope() as db:
            row = db.get(TradeLifecycleRecord, ticket)
            if not row:
                return
            log = json.loads(row.management_log)
            log.append({"timestamp": utcnow().isoformat(), "action": action, "reason": reason, "result": result})
            row.management_log = json.dumps(log, default=str)
            if action == "CLOSE" and result.get("reconciled"):
                row.exit_reason = reason

    def record_trade_outcome(self, ticket: str, reason: str, result: dict, final_r: float, duration_seconds: float) -> None:
        raw = result.get("raw") or {}
        deal = raw.get("deal") or {}
        with session_scope() as db:
            row = db.get(TradeOutcomeRecord, ticket) or TradeOutcomeRecord(ticket=ticket, exit_reason=reason)
            row.closed_at = utcnow()
            row.exit_price = deal.get("price") or raw.get("exit_price")
            row.realized_pnl = deal.get("profit") if deal else raw.get("realized_pnl")
            row.commission = deal.get("commission") if deal else raw.get("commission")
            row.swap = deal.get("swap") if deal else raw.get("swap")
            row.final_r = final_r
            row.duration_seconds = duration_seconds
            row.exit_reason = reason
            row.broker_payload = json.dumps(result, default=str)
            db.add(row)

