"""Sidecar → hosted-tier shipping, tested against a live in-process ingest."""

import asyncio
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from infersight.config import InferSightConfig
from infersight.forwarder import HostedForwarder
from infersight.metrics import RequestRecord


received: list[dict] = []
auth_seen: list[str] = []
engine_states: list[dict] = []


def _fake_ingest_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/ingest")
    async def ingest(request: Request, authorization: str = Header(default="")):
        auth_seen.append(authorization)
        body = await request.json()
        received.extend(body["records"])
        return JSONResponse({"accepted": len(body["records"])})

    @app.post("/v1/ingest/engine-state")
    async def engine_state(request: Request):
        engine_states.append(await request.json())
        return JSONResponse({"ok": True})

    return app


@pytest.fixture(scope="module")
def ingest_server():
    import socket
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(_fake_ingest_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _rec(i: int) -> RequestRecord:
    return RequestRecord(
        ts=time.time(), model=f"m{i}", endpoint="/v1/completions", engine="vllm",
        status="200", e2e_seconds=1.0, ttft_seconds=0.1, prompt_tokens=10,
        completion_tokens=20, streamed=False,
    )


def test_forwarder_ships_batches(ingest_server):
    cfg = InferSightConfig(hosted={
        "enabled": True,
        "ingest_url": f"{ingest_server}/v1/ingest",
        "api_key": "isk_secret",
        "max_batch_size": 10,
    })
    fwd = HostedForwarder(cfg)
    for i in range(25):
        fwd.enqueue(_rec(i))

    async def drain():
        total = 0
        for _ in range(5):
            total += await fwd.flush()
        return total

    sent = asyncio.run(drain())
    assert sent == 25
    assert len(received) == 25
    assert received[0]["model"] == "m0"
    assert auth_seen[0] == "Bearer isk_secret"


def test_engine_state_shipped(ingest_server):
    """The hosted KV-cache alert depends on this path — it must not be dead."""
    cfg = InferSightConfig(hosted={
        "enabled": True,
        "ingest_url": f"{ingest_server}/v1/ingest",
        "api_key": "isk_secret",
    })
    fwd = HostedForwarder(cfg)
    asyncio.run(fwd.send_engine_state(
        kv_cache_usage_ratio=0.93, queue_running=4, queue_waiting=2, queue_swapped=0,
    ))
    assert engine_states and engine_states[-1]["kv_cache_usage_ratio"] == 0.93


def test_forwarder_requeues_on_failure():
    cfg = InferSightConfig(hosted={
        "enabled": True,
        "ingest_url": "http://127.0.0.1:1/v1/ingest",  # dead endpoint
        "api_key": "isk_secret",
    })
    fwd = HostedForwarder(cfg)
    fwd.enqueue(_rec(0))
    sent = asyncio.run(fwd.flush())
    assert sent == 0
    assert len(fwd._queue) == 1  # requeued, not lost
