"""InferSight hosted tier — multi-tenant ingest + dashboard API.

Endpoints
---------
POST /v1/ingest                 sidecar batches (Bearer API key → tenant)
POST /v1/ingest/engine-state    engine gauge snapshots (KV cache, queue)
GET  /v1/api/summary            headline stats for the dashboard
GET  /v1/api/timeseries         bucketed request/latency series
GET  /v1/api/models             multi-model / multi-cluster comparison
GET  /v1/api/alerts             recent alert events
POST /v1/admin/tenants          create a tenant (admin token)
GET  /                          the hosted dashboard UI

Run:  uvicorn hosted.ingest:app --port 9000
Env:  INFERSIGHT_HOSTED_DB, INFERSIGHT_ADMIN_TOKEN,
      INFERSIGHT_SLACK_WEBHOOK, INFERSIGHT_PAGERDUTY_KEY
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from hosted.alerting import AlertEngine, AlertRuleConfig, evaluation_loop
from hosted.storage import MetricStore, Tenant

DB_PATH = os.environ.get("INFERSIGHT_HOSTED_DB", "infersight-hosted.db")
ADMIN_TOKEN = os.environ.get("INFERSIGHT_ADMIN_TOKEN") or "admin_" + secrets.token_urlsafe(16)
STATIC_DIR = Path(__file__).parent / "static"

store = MetricStore(DB_PATH)
alert_engine = AlertEngine(store, AlertRuleConfig(
    slack_webhook_url=os.environ.get("INFERSIGHT_SLACK_WEBHOOK", ""),
    pagerduty_routing_key=os.environ.get("INFERSIGHT_PAGERDUTY_KEY", ""),
))


RETENTION_SECONDS = float(os.environ.get("INFERSIGHT_RETENTION_DAYS", "14")) * 86400


async def _prune_loop() -> None:
    while True:
        try:
            removed = store.prune(RETENTION_SECONDS)
            if removed:
                print(f"[infersight-hosted] pruned {removed} records past retention")
        except asyncio.CancelledError:
            return
        except Exception as exc:  # never kill the loop
            print(f"[infersight-hosted] prune failed: {exc}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("INFERSIGHT_ADMIN_TOKEN"):
        print(f"[infersight-hosted] generated admin token: {ADMIN_TOKEN}")
    tasks = [
        asyncio.create_task(evaluation_loop(alert_engine)),
        asyncio.create_task(_prune_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="InferSight Hosted", lifespan=lifespan)


# ---------------------------------------------------------------------- #
# Auth
# ---------------------------------------------------------------------- #


def tenant_from_auth(authorization: str = Header(default="")) -> Tenant:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer API key")
    tenant = store.tenant_by_key(authorization.removeprefix("Bearer ").strip())
    if not tenant:
        raise HTTPException(403, "Unknown API key")
    return tenant


def require_admin(authorization: str = Header(default="")) -> None:
    if authorization.removeprefix("Bearer ").strip() != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")


# ---------------------------------------------------------------------- #
# Ingest
# ---------------------------------------------------------------------- #


class IngestBatch(BaseModel):
    records: list[dict] = Field(default_factory=list)
    cluster: str = "default"


class EngineState(BaseModel):
    kv_cache_usage_ratio: Optional[float] = None
    queue_running: Optional[float] = None
    queue_waiting: Optional[float] = None
    queue_swapped: Optional[float] = None


@app.post("/v1/ingest")
async def ingest(batch: IngestBatch, tenant: Tenant = Depends(tenant_from_auth)) -> JSONResponse:
    if len(batch.records) > 5000:
        raise HTTPException(413, "Batch too large (max 5000 records)")
    accepted, rejected = store.insert_records(tenant.id, batch.records, cluster=batch.cluster)
    return JSONResponse({"accepted": accepted, "rejected": rejected})


@app.post("/v1/ingest/engine-state")
async def ingest_engine_state(state: EngineState, tenant: Tenant = Depends(tenant_from_auth)) -> JSONResponse:
    alert_engine.update_engine_state(tenant.id, {
        "kv_cache_usage_ratio": state.kv_cache_usage_ratio,
        "queue_running": state.queue_running,
        "queue_waiting": state.queue_waiting,
        "queue_swapped": state.queue_swapped,
    })
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------- #
# Dashboard API
# ---------------------------------------------------------------------- #


@app.get("/v1/api/summary")
async def api_summary(
    tenant: Tenant = Depends(tenant_from_auth),
    window: float = Query(3600, gt=0, le=7 * 86400),
) -> JSONResponse:
    return JSONResponse({"tenant": tenant.name, **store.summary(tenant.id, window)})


@app.get("/v1/api/timeseries")
async def api_timeseries(
    tenant: Tenant = Depends(tenant_from_auth),
    window: float = Query(3600, gt=0, le=7 * 86400),
    bucket: int = Query(60, gt=0, le=86400),
) -> JSONResponse:
    return JSONResponse({"series": store.timeseries(tenant.id, window, bucket)})


@app.get("/v1/api/models")
async def api_models(
    tenant: Tenant = Depends(tenant_from_auth),
    window: float = Query(3600, gt=0, le=7 * 86400),
) -> JSONResponse:
    return JSONResponse({"models": store.model_comparison(tenant.id, window)})


@app.get("/v1/api/alerts")
async def api_alerts(tenant: Tenant = Depends(tenant_from_auth)) -> JSONResponse:
    return JSONResponse({"alerts": store.recent_alerts(tenant.id)})


# ---------------------------------------------------------------------- #
# Admin
# ---------------------------------------------------------------------- #


class CreateTenant(BaseModel):
    name: str


@app.post("/v1/admin/tenants", dependencies=[Depends(require_admin)])
async def create_tenant(body: CreateTenant) -> JSONResponse:
    tenant = store.create_tenant(body.name)
    return JSONResponse({"id": tenant.id, "name": tenant.name, "api_key": tenant.api_key})


# ---------------------------------------------------------------------- #
# Dashboard UI
# ---------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text())
