# Known limitations (v0.1)

Honest gaps, deliberately deferred — with the upgrade path for each.

**Measurement**
- TBT is measured per *network read*, not per engine step. If vLLM flushes
  several tokens in one write (common under heavy batching), those tokens
  register as one gap. Direction is still correct for regressions; absolute
  values can undercount under load. Fix path: engine-side step timing via a
  vLLM plugin.
- Chunk-count token fallback (when a server lacks `stream_options`
  include_usage) approximates tokens as SSE events; multi-token chunks
  undercount. Exact counts require usage reporting.
- Injecting `include_usage` adds one final `choices: []` usage chunk to the
  client's stream. Standard OpenAI-spec behavior, but disable with
  `inject_stream_usage: false` if a client SDK chokes on it.

**Sidecar**
- The model-label cardinality cap (`__other__` overflow) is process-local
  in-memory; a restart resets which models grabbed the slots.
- Non-completion endpoints are proxied buffered, not streamed.
- Upstream response headers beyond content-type are not forwarded.

**Hosted tier**
- SQLite + single worker: run exactly one uvicorn worker. The admin token is
  generated per-process if unset — set `INFERSIGHT_ADMIN_TOKEN` explicitly in
  any real deployment. Postgres/ClickHouse is the seam for scale.
- Percentiles are exact but computed via COUNT+OFFSET per query — fine at MVP
  volume, needs pre-aggregation past ~10⁶ rows/window.
- Engine-state (KV cache) is latest-write-wins per tenant; multiple replicas
  shipping simultaneously overwrite each other. Fix path: key by
  cluster+replica.
- No rate limiting or TLS termination built in — front with a reverse proxy.
- Alert evaluation runs in a single loop; per-tenant rule customization is
  not yet exposed (constants in `AlertRuleConfig`).

**Engines**
- TGI and SGLang get request-level timing today; their engine gauges
  (KV cache, queue) need parser additions in `_ENGINE_METRIC_PATTERNS`.
