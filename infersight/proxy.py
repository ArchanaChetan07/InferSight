"""The InferSight sidecar proxy.

Sits between clients and a vLLM (or any OpenAI-compatible) server:

    client ──► infersight :8020 ──► vLLM :8000

For streaming requests the SSE byte stream is passed through untouched while
chunk timestamps are recorded out-of-band, so the measurement overhead on the
hot path is two perf_counter() reads per chunk.

Also scrapes the engine's own /metrics endpoint in the background to surface
KV-cache pressure and scheduler queue depth — the signals that precede OOMs
and latency cliffs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from infersight import __version__
from infersight.config import InferSightConfig
from infersight.forwarder import HostedForwarder
from infersight.metrics import MetricsRegistry, RequestRecord, StreamTimer

log = logging.getLogger("infersight.proxy")

COMPLETION_ENDPOINTS = {"/v1/completions", "/v1/chat/completions"}

# vLLM engine metric names we translate into InferSight gauges.
_ENGINE_METRIC_PATTERNS = {
    "kv_cache": re.compile(r'^vllm:gpu_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)'),
    "running": re.compile(r'^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)'),
    "waiting": re.compile(r'^vllm:num_requests_waiting(?:\{[^}]*\})?\s+([0-9.eE+-]+)'),
    "swapped": re.compile(r'^vllm:num_requests_swapped(?:\{[^}]*\})?\s+([0-9.eE+-]+)'),
}


def create_app(config: InferSightConfig) -> FastAPI:
    metrics = MetricsRegistry(config)
    forwarder = HostedForwarder(config) if config.hosted.enabled else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(
            base_url=config.upstream_url,
            timeout=httpx.Timeout(config.request_timeout_seconds, connect=10.0),
        )
        tasks: list[asyncio.Task] = []
        if config.scrape_engine_metrics:
            tasks.append(asyncio.create_task(
                _engine_scrape_loop(app.state.client, config, metrics, forwarder)
            ))
        if forwarder:
            tasks.append(asyncio.create_task(forwarder.run()))
        yield
        for t in tasks:
            t.cancel()
        if forwarder:
            await forwarder.flush(final=True)
            await forwarder.aclose()
        await app.state.client.aclose()

    app = FastAPI(title="InferSight", version=__version__, lifespan=lifespan)
    app.state.metrics = metrics
    app.state.config = config
    app.state.forwarder = forwarder

    # ------------------------------------------------------------------ #
    # Local endpoints
    # ------------------------------------------------------------------ #

    @app.get(config.metrics_path)
    async def prometheus_metrics() -> Response:
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    @app.get("/infersight/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "upstream": config.upstream_url})

    # ------------------------------------------------------------------ #
    # Proxy
    # ------------------------------------------------------------------ #

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        endpoint = "/" + path

        if request.method == "POST" and endpoint in COMPLETION_ENDPOINTS:
            return await _proxy_completion(request, client, endpoint, config, metrics, forwarder)
        return await _proxy_passthrough(request, client, endpoint)

    return app


# ---------------------------------------------------------------------- #
# Completion proxying with token-level instrumentation
# ---------------------------------------------------------------------- #


async def _proxy_completion(
    request: Request,
    client: httpx.AsyncClient,
    endpoint: str,
    config: InferSightConfig,
    metrics: MetricsRegistry,
    forwarder: Optional[HostedForwarder],
) -> Response:
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {}

    model = _bounded_model_label(
        config.model_label or str(payload.get("model", "unknown")), config
    )
    stream = bool(payload.get("stream", False))
    labels = dict(model=model, endpoint=endpoint, engine=config.engine)
    headers = _forward_headers(request)
    params = dict(request.query_params)

    metrics.in_flight.labels(engine=config.engine).inc()
    timer = StreamTimer()

    if not stream:
        try:
            try:
                upstream = await client.post(endpoint, content=body, headers=headers, params=params)
            except httpx.HTTPError as exc:
                rec = _record(timer, labels, status="upstream_error", streamed=False)
                metrics.observe_request(rec)
                _enqueue(forwarder, rec)
                return JSONResponse({"error": f"upstream unreachable: {exc}"}, status_code=502)

            prompt_toks, completion_toks = _usage_from_json(upstream)
            rec = _record(
                timer, labels,
                status=str(upstream.status_code),
                streamed=False,
                prompt_tokens=prompt_toks,
                completion_tokens=completion_toks,
            )
            metrics.observe_request(rec)
            _enqueue(forwarder, rec)
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )
        finally:
            metrics.in_flight.labels(engine=config.engine).dec()

    # --- streaming (SSE) path ---
    # Ask vLLM to include usage in the final chunk so token counts are exact.
    if (
        config.inject_stream_usage
        and isinstance(payload, dict)
        and "stream_options" not in payload
    ):
        payload["stream_options"] = {"include_usage": True}
        body = json.dumps(payload).encode()
        headers.pop("content-length", None)

    req = client.build_request("POST", endpoint, content=body, headers=headers, params=params)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        metrics.in_flight.labels(engine=config.engine).dec()
        rec = _record(timer, labels, status="upstream_error", streamed=True)
        metrics.observe_request(rec)
        _enqueue(forwarder, rec)
        return JSONResponse({"error": f"upstream unreachable: {exc}"}, status_code=502)

    async def instrumented() -> AsyncIterator[bytes]:
        prompt_toks = 0
        completion_toks = 0
        content_chunks = 0
        usage_chunks = 0
        status = str(upstream.status_code)
        sse = SSELineBuffer()
        try:
            async for raw in upstream.aiter_bytes():
                # Timing first — parsing must never delay the client. One gap
                # observation per network read: lines batched into a single
                # read arrived together, so per-line gaps would record
                # meaningless near-zero TBTs.
                lines = sse.feed(raw)
                if lines:
                    gap = timer.on_chunk()
                    if gap is not None:
                        metrics.observe_tbt(gap_seconds=gap, **labels)
                for data in lines:
                    if b'"usage"' in data:
                        try:
                            usage = json.loads(data).get("usage") or {}
                            if usage:
                                usage_chunks += 1
                                prompt_toks = int(usage.get("prompt_tokens") or 0)
                                completion_toks = int(usage.get("completion_tokens") or 0)
                                continue
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                    content_chunks += 1
                yield raw
        except (httpx.HTTPError, asyncio.CancelledError):
            status = "stream_aborted"
            raise
        finally:
            await upstream.aclose()
            metrics.in_flight.labels(engine=config.engine).dec()
            rec = _record(
                timer, labels,
                status=status,
                streamed=True,
                prompt_tokens=prompt_toks,
                # Exact count from usage when available; otherwise content
                # chunk count (≈ tokens for vLLM's per-token SSE).
                completion_tokens=completion_toks or content_chunks,
            )
            metrics.observe_request(rec)
            _enqueue(forwarder, rec)

    return StreamingResponse(
        instrumented(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


async def _proxy_passthrough(request: Request, client: httpx.AsyncClient, endpoint: str) -> Response:
    try:
        upstream = await client.request(
            request.method,
            endpoint,
            params=dict(request.query_params),
            content=await request.body(),
            headers=_forward_headers(request),
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"upstream unreachable: {exc}"}, status_code=502)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


class SSELineBuffer:
    """Reassembles SSE `data:` lines that may be split across network reads.

    An SSE event boundary is a newline, but aiter_bytes() yields transport
    chunks — a line can arrive as `da` + `ta: {...}\\n`. Feeding raw bytes
    returns only *complete* data payloads (excluding `[DONE]` and blanks);
    the trailing partial line is kept for the next read.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> list[bytes]:
        self._buf += raw
        *complete, self._buf = self._buf.split(b"\n")
        out: list[bytes] = []
        for line in complete:
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data and data != b"[DONE]":
                out.append(data)
        return out


# Bounded model-label tracking: untrusted clients can send arbitrary `model`
# strings; unbounded label values would blow up Prometheus cardinality.
_seen_models: set[str] = set()


def _bounded_model_label(model: str, config: InferSightConfig) -> str:
    model = model[:200]
    if model in _seen_models:
        return model
    if len(_seen_models) >= config.max_model_labels:
        return "__other__"
    _seen_models.add(model)
    return model


# ---------------------------------------------------------------------- #
# Engine metric scraping (KV cache / queue depth)
# ---------------------------------------------------------------------- #


async def _engine_scrape_loop(
    client: httpx.AsyncClient,
    config: InferSightConfig,
    metrics: MetricsRegistry,
    forwarder: Optional[HostedForwarder] = None,
) -> None:
    while True:
        try:
            resp = await client.get("/metrics")
            if resp.status_code == 200:
                values = parse_engine_metrics(resp.text)
                metrics.set_engine_gauges(
                    upstream=config.upstream_url,
                    engine=config.engine,
                    kv_cache_ratio=values.get("kv_cache"),
                    running=values.get("running"),
                    waiting=values.get("waiting"),
                    swapped=values.get("swapped"),
                )
                # Feed the hosted tier's KV-cache/queue alert rules.
                if forwarder and values:
                    await forwarder.send_engine_state(
                        kv_cache_usage_ratio=values.get("kv_cache"),
                        queue_running=values.get("running"),
                        queue_waiting=values.get("waiting"),
                        queue_swapped=values.get("swapped"),
                    )
        except httpx.HTTPError:
            log.debug("engine /metrics scrape failed; will retry")
        except asyncio.CancelledError:
            return
        await asyncio.sleep(config.engine_metrics_interval_seconds)


def parse_engine_metrics(text: str) -> dict[str, float]:
    """Extract KV-cache and queue gauges from a vLLM Prometheus exposition."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        for key, pattern in _ENGINE_METRIC_PATTERNS.items():
            if key in out:
                continue
            m = pattern.match(line)
            if m:
                out[key] = float(m.group(1))
    return out


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #

_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}


def _usage_from_json(response: httpx.Response) -> tuple[int, int]:
    try:
        usage = response.json().get("usage") or {}
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return 0, 0


def _record(timer: StreamTimer, labels: dict, status: str, streamed: bool,
            prompt_tokens: int = 0, completion_tokens: int = 0) -> RequestRecord:
    return RequestRecord(
        ts=time.time(),
        model=labels["model"],
        endpoint=labels["endpoint"],
        engine=labels["engine"],
        status=status,
        e2e_seconds=timer.e2e,
        ttft_seconds=timer.ttft,
        tbt_seconds_mean=timer.tbt_mean,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        streamed=streamed,
    )


def _enqueue(forwarder: Optional[HostedForwarder], rec: RequestRecord) -> None:
    if forwarder:
        forwarder.enqueue(rec)
