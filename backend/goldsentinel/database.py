from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings
from .domain import RuntimeSettings, utcnow


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

    def equity(self, start: datetime | None = None, source: str | None = None) -> list[EquitySnapshot]:
        with session_scope() as db:
            stmt = select(EquitySnapshot)
            if start:
                stmt = stmt.where(EquitySnapshot.timestamp >= start)
            if source:
                stmt = stmt.where(EquitySnapshot.source == source)
            return list(db.scalars(stmt.order_by(EquitySnapshot.timestamp)).all())

