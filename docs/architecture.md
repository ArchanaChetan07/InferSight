# Architecture

```
                       ┌────────────────────────────────────────┐
   client traffic ───► │ InferSight sidecar (FastAPI, :8020)    │ ───► vLLM (:8000)
                       │                                        │
                       │  • SSE stream passthrough              │ ◄─── /metrics scrape
                       │    (timing recorded out-of-band)       │      (KV cache, queue)
                       │  • StreamTimer: 2 perf_counter reads   │
                       │    per chunk, __slots__, no parsing    │
                       │    on the critical path                │
                       └───────┬───────────────────┬────────────┘
                               │                   │
                    /metrics   │                   │  batched JSON, bounded queue,
                  (Prometheus) │                   │  drop-oldest, fail-open
                               ▼                   ▼
                     Grafana dashboards    hosted ingest (FastAPI, :9000)
                     (import-ready JSON)     │  API key → tenant (server-side only)
                                             ├─ SQLite store (swap: Postgres/ClickHouse)
                                             ├─ dashboard UI + JSON API
                                             └─ alert engine → Slack / PagerDuty
```

## Design decisions

**Proxy, not fork.** Instrumenting at the HTTP boundary means zero changes to
vLLM, works across engine versions, and extends to TGI/SGLang by adding gauge
parsers — the OpenAI-compatible surface is the stable contract.

**Timing before parsing.** On the streaming path, chunk timestamps are taken
before any JSON work, and the client's bytes are yielded unmodified. The only
payload inspection is a substring check for `"usage"` on each data line.

**Exact token counts.** The sidecar injects `stream_options: {include_usage:
true}` when absent, so vLLM reports exact prompt/completion tokens on the final
chunk; chunk-count is the fallback for servers that don't support it.

**Tenancy is server-side.** The hosted tier resolves tenant identity only from
the API key. Client-supplied tenant fields are ignored; every query is scoped
by `tenant_id` at the storage layer.

**Fail-open shipping.** The forwarder can never back-pressure inference:
enqueue is O(1) into a bounded deque, flushes happen on a background task, and
a dead ingest endpoint costs nothing but dropped telemetry.

**SQLite first.** One file, zero ops for the MVP; the `MetricStore` interface
is the seam for Postgres or ClickHouse when write volume demands it.

## Data model

One row per completed request:

`ts, model, endpoint, engine, cluster, status, e2e_seconds, ttft_seconds,
tbt_seconds_mean, prompt_tokens, completion_tokens, streamed`

Percentiles are computed at query time (COUNT + OFFSET), which is exact and
fine at MVP scale; pre-aggregation buckets are the upgrade path.
