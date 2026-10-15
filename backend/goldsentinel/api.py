from __future__ import annotations

import csv
import io
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import get_settings
from .database import Repository, init_db
from .domain import OrderRequest, RuntimeSettings, StrategyProfile, Timeframe
from .performance import report
from .service import GoldSentinelService


settings = get_settings()
init_db()
repository = Repository()
service = GoldSentinelService(settings, repository=repository)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    service.start()
    async def scan_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(service.analyze, Timeframe.m15)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                repository.audit("SYSTEM", "Background analysis failed", {"error": str(exc)}, "ERROR")
            await asyncio.sleep(settings.polling_seconds)
    async def news_loop() -> None:
        while True:
            try:
                await service.news.refresh()
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(
                min(settings.news_refresh_seconds, settings.headline_refresh_seconds)
            )
    async def management_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(service.manage_positions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                repository.audit("POSITION_MANAGEMENT", "Background management failed", {"error": str(exc)}, "ERROR")
            await asyncio.sleep(settings.position_manage_seconds)
    async def history_refresh() -> None:
        try:
            await asyncio.to_thread(service.refresh_history)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            repository.audit("HISTORY", "Startup history refresh failed", {"error": str(exc)}, "ERROR")
    task = asyncio.create_task(scan_loop(), name="goldsentinel-scan-loop")
    news_task = asyncio.create_task(news_loop(), name="goldsentinel-news-loop")
    management_task = asyncio.create_task(management_loop(), name="goldsentinel-position-management-loop")
    history_task = asyncio.create_task(history_refresh(), name="goldsentinel-history-refresh") if settings.history_refresh_on_startup else None
    try:
        yield
    finally:
        task.cancel()
        news_task.cancel()
        management_task.cancel()
        if history_task and not history_task.done():
            history_task.cancel()
        try:
            await asyncio.gather(task, news_task, management_task, *([history_task] if history_task else []))
        except asyncio.CancelledError:
            pass
        service.stop()


app = FastAPI(title="GoldSentinel API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def require_token(authorization: str | None = Header(default=None), x_api_token: str | None = Header(default=None)) -> None:
    supplied = x_api_token or (authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else None)
    if not supplied or supplied != settings.api_token:
        raise HTTPException(status_code=401, detail="Valid API token required")


class ArmRequest(BaseModel):
    armed: bool


class PauseRequest(BaseModel):
    paused: bool


class ApprovalRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


class ModificationRequest(BaseModel):
    stop_loss: float
    take_profit: float


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "broker_connected": service.broker.is_connected()}


@app.get("/api/status")
def status() -> dict:
    return service.status()


@app.get("/api/quote")
def quote() -> dict:
    return service.quote_snapshot()


@app.get("/api/market/{timeframe}")
def market(timeframe: Timeframe, count: int = Query(350, ge=50, le=5000), offset: int = Query(0, ge=0)) -> dict:
    return service.market_snapshot(timeframe, count, offset)


@app.get("/api/market/{timeframe}/live")
def live_market(timeframe: Timeframe) -> dict:
    return service.live_market(timeframe)


@app.get("/api/market-diagnostics")
def market_diagnostics(count: int = Query(350, ge=50, le=5000)) -> dict:
    return service.market_diagnostics(count)


@app.get("/api/market-context")
def market_context() -> dict:
    return service.market_context().model_dump()


@app.post("/api/history/refresh")
def refresh_history(_: None = Depends(require_token)) -> dict:
    return service.refresh_history()


@app.post("/api/analyze/{timeframe}")
def analyze(timeframe: Timeframe, _: None = Depends(require_token)) -> dict:
    return service.analyze(timeframe).model_dump()


@app.put("/api/settings")
def update_settings(value: RuntimeSettings, _: None = Depends(require_token)) -> dict:
    return service.update_runtime(value).model_dump()


@app.post("/api/arm")
def arm(value: ArmRequest, _: None = Depends(require_token)) -> dict:
    try:
        return service.set_armed(value.armed).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/pause")
def pause(value: PauseRequest, _: None = Depends(require_token)) -> dict:
    return service.set_paused(value.paused).model_dump()


@app.post("/api/emergency-stop")
def emergency_stop(_: None = Depends(require_token)) -> dict:
    return service.emergency_stop()


@app.post("/api/emergency-reset")
def emergency_reset(_: None = Depends(require_token)) -> dict:
    return service.reset_emergency_stop().model_dump()


@app.post("/api/candidates/{candidate_id}/approve")
def approve(candidate_id: str, value: ApprovalRequest, _: None = Depends(require_token)) -> dict:
    try:
        return service.approve(candidate_id, value.idempotency_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/candidates/{candidate_id}/reject")
def reject(candidate_id: str, _: None = Depends(require_token)) -> dict:
    candidate = service.candidates.get(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    candidate.status = "REJECTED_BY_USER"
    repository.audit("SIGNAL", "Candidate rejected by user", {"candidate_id": candidate_id})
    return candidate.model_dump()


@app.post("/api/manual-order")
def manual_order(order: OrderRequest, _: None = Depends(require_token)) -> dict:
    if service.runtime.emergency_stop or service.runtime.paused:
        raise HTTPException(status_code=409, detail="Trading is blocked by the current safety state")
    account = service.broker.account()
    if not account.is_demo:
        raise HTTPException(status_code=409, detail="Only demo execution is supported")
    result = service.broker.submit_market_order(settings.symbol, order)
    repository.audit("MANUAL_ORDER", result.message, {**order.model_dump(), **result.model_dump()})
    return result.model_dump()


@app.post("/api/positions/{ticket}/close")
def close_position(ticket: str, volume: float | None = None, _: None = Depends(require_token)) -> dict:
    return service.broker.close_position(ticket, volume).model_dump()


@app.put("/api/positions/{ticket}")
def modify_position(ticket: str, value: ModificationRequest, _: None = Depends(require_token)) -> dict:
    return service.broker.modify_position(ticket, value.stop_loss, value.take_profit).model_dump()


@app.delete("/api/orders/{ticket}")
def cancel_order(ticket: str, _: None = Depends(require_token)) -> dict:
    return service.broker.cancel_pending(ticket).model_dump()


@app.delete("/api/orders")
def cancel_all_orders(_: None = Depends(require_token)) -> dict:
    return {"results": [service.broker.cancel_pending(str(order.get("ticket", order.get("order", "")))).model_dump() for order in service.broker.pending_orders()]}


@app.get("/api/logs")
def logs(limit: int = Query(200, ge=1, le=2000)) -> list[dict]:
    return repository.audit_events(limit)


@app.get("/api/performance")
def performance(period: str = "all", start: datetime | None = None, end: datetime | None = None, source: str | None = None) -> dict:
    return report(repository, period, start, end, source)


@app.get("/api/performance/export.csv")
def export_trades(period: str = "all", source: str | None = None) -> Response:
    data = report(repository, period, source=source)["trades"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(data[0].keys()) if data else ["ticket", "source", "side", "volume", "entry", "exit", "net_pnl", "opened_at", "closed_at", "timeframe", "setup", "session"])
    writer.writeheader()
    writer.writerows(data)
    return Response(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=goldsentinel-trades.csv"})


@app.post("/api/news/refresh")
async def refresh_news(_: None = Depends(require_token)) -> dict:
    await service.news.refresh()
    return service.news.status(service.runtime.risk.news_policy)


@app.post("/api/backtest")
def backtest(timeframe: Timeframe = Timeframe.m15, profile: StrategyProfile = StrategyProfile.balanced, count: int = Query(2500, ge=200, le=10000), walk_forward_mode: bool = False, _: None = Depends(require_token)) -> dict:
    return service.backtest(timeframe, profile, count, walk_forward_mode)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("goldsentinel.api:app", host="127.0.0.1", port=8000, reload=True)

