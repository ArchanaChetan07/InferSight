"""LLM-native Prometheus metrics.

These are the metrics generic APM misses:

    infersight_ttft_seconds            time-to-first-token histogram
    infersight_tbt_seconds             time-between-tokens histogram
    infersight_e2e_latency_seconds     end-to-end request latency histogram
    infersight_prompt_tokens_total     prompt tokens counter
    infersight_completion_tokens_total completion tokens counter
    infersight_requests_total          requests counter (by status)
    infersight_requests_in_flight      gauge
    infersight_kv_cache_usage_ratio    gauge (scraped from vLLM engine)
    infersight_queue_depth             gauge (running/waiting/swapped)

All request metrics are labelled by model and endpoint so multi-model
deployments can be compared side by side.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from infersight.config import InferSightConfig

REQUEST_LABELS = ["model", "endpoint", "engine"]


@dataclass
class RequestRecord:
    """A single completed request — the unit shipped to the hosted tier."""

    ts: float
    model: str
    endpoint: str
    engine: str
    status: str
    e2e_seconds: float
    ttft_seconds: Optional[float] = None
    tbt_seconds_mean: Optional[float] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    streamed: bool = False

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "model": self.model,
            "endpoint": self.endpoint,
            "engine": self.engine,
            "status": self.status,
            "e2e_seconds": self.e2e_seconds,
            "ttft_seconds": self.ttft_seconds,
            "tbt_seconds_mean": self.tbt_seconds_mean,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "streamed": self.streamed,
        }


class MetricsRegistry:
    """Owns the Prometheus registry and records per-request observations."""

    def __init__(self, config: InferSightConfig) -> None:
        self.config = config
        self.registry = CollectorRegistry()

        self.ttft = Histogram(
            "infersight_ttft_seconds",
            "Time to first token",
            REQUEST_LABELS,
            buckets=config.ttft_buckets,
            registry=self.registry,
        )
        self.tbt = Histogram(
            "infersight_tbt_seconds",
            "Time between tokens (per inter-token gap)",
            REQUEST_LABELS,
            buckets=config.tbt_buckets,
            registry=self.registry,
        )
        self.e2e = Histogram(
            "infersight_e2e_latency_seconds",
            "End-to-end request latency",
            REQUEST_LABELS,
            buckets=config.e2e_buckets,
            registry=self.registry,
        )
        self.prompt_tokens = Counter(
            "infersight_prompt_tokens_total",
            "Prompt tokens processed",
            REQUEST_LABELS,
            registry=self.registry,
        )
        self.completion_tokens = Counter(
            "infersight_completion_tokens_total",
            "Completion tokens generated",
            REQUEST_LABELS,
            registry=self.registry,
        )
        self.requests = Counter(
            "infersight_requests_total",
            "Proxied requests",
            REQUEST_LABELS + ["status"],
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "infersight_requests_in_flight",
            "Requests currently being served",
            ["engine"],
            registry=self.registry,
        )
        self.kv_cache_usage = Gauge(
            "infersight_kv_cache_usage_ratio",
            "KV-cache utilization scraped from the engine (0..1)",
            ["engine", "upstream"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "infersight_queue_depth",
            "Engine scheduler queue depth",
            ["engine", "upstream", "state"],  # state: running | waiting | swapped
            registry=self.registry,
        )

    # ------------------------------------------------------------------ #

    def observe_request(self, rec: RequestRecord) -> None:
        labels = dict(model=rec.model, endpoint=rec.endpoint, engine=rec.engine)
        self.requests.labels(**labels, status=rec.status).inc()
        self.e2e.labels(**labels).observe(rec.e2e_seconds)
        if rec.ttft_seconds is not None:
            self.ttft.labels(**labels).observe(rec.ttft_seconds)
        if rec.prompt_tokens:
            self.prompt_tokens.labels(**labels).inc(rec.prompt_tokens)
        if rec.completion_tokens:
            self.completion_tokens.labels(**labels).inc(rec.completion_tokens)

    def observe_tbt(self, model: str, endpoint: str, engine: str, gap_seconds: float) -> None:
        self.tbt.labels(model=model, endpoint=endpoint, engine=engine).observe(gap_seconds)

    def set_engine_gauges(
        self,
        upstream: str,
        engine: str,
        kv_cache_ratio: Optional[float],
        running: Optional[float],
        waiting: Optional[float],
        swapped: Optional[float],
    ) -> None:
        if kv_cache_ratio is not None:
            self.kv_cache_usage.labels(engine=engine, upstream=upstream).set(kv_cache_ratio)
        for state, value in (("running", running), ("waiting", waiting), ("swapped", swapped)):
            if value is not None:
                self.queue_depth.labels(engine=engine, upstream=upstream, state=state).set(value)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class StreamTimer:
    """Tracks token timing for one streamed response with near-zero overhead.

    Uses time.perf_counter() for monotonic sub-millisecond deltas; only two
    floats and two ints are stored per request on the hot path.
    """

    __slots__ = ("start", "first_token_at", "last_token_at", "chunks", "gap_sum")

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.first_token_at: Optional[float] = None
        self.last_token_at: Optional[float] = None
        self.chunks = 0
        self.gap_sum = 0.0

    def on_chunk(self) -> Optional[float]:
        """Record a content chunk. Returns the inter-token gap, if any."""
        now = time.perf_counter()
        gap: Optional[float] = None
        if self.first_token_at is None:
            self.first_token_at = now
        elif self.last_token_at is not None:
            gap = now - self.last_token_at
            self.gap_sum += gap
        self.last_token_at = now
        self.chunks += 1
        return gap

    @property
    def ttft(self) -> Optional[float]:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.start

    @property
    def tbt_mean(self) -> Optional[float]:
        if self.chunks < 2:
            return None
        return self.gap_sum / (self.chunks - 1)

    @property
    def e2e(self) -> float:
        return time.perf_counter() - self.start
