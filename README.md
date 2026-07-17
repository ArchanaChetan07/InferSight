# InferSight

**Purpose-built observability for self-hosted LLM inference.**

Your APM dashboard says CPU is fine, memory is fine, requests are returning 200 — and your users are staring at a spinner for four seconds before the first token appears. Generic observability can't see how LLM serving actually fails.

InferSight is a lightweight sidecar for vLLM (and any OpenAI-compatible server) that captures the metrics that matter for inference:

| Metric | Why it matters |
| --- | --- |
| **TTFT** (time to first token) | The latency your users actually feel |
| **TBT** (time between tokens) | Streaming smoothness; decode-phase health |
| **E2E latency** | Full request wall time, P50–P99 |
| **KV-cache usage** | The gauge that predicts preemptions and OOMs |
| **Queue depth** | running / waiting / swapped scheduler state |
| **Token throughput** | Exact prompt + completion counts per model |

Everything is exposed as Prometheus metrics, with import-ready Grafana dashboards included — no panels to build from scratch.

## Install in 5 minutes

```bash
pip install infersight

# vLLM already running on :8000? Point the sidecar at it:
infersight run --upstream http://localhost:8000 --port 8020
```

Send your traffic to `:8020` instead of `:8000` (it's a transparent proxy — same API, streaming included), and scrape `http://localhost:8020/metrics` with Prometheus. Import `dashboards/infersight-vllm.json` into Grafana. Done.

Not sure where your inference servers are?

```bash
infersight discover        # probes for OpenAI-compatible / vLLM servers
```

### Or run the whole demo stack (no GPU needed)

```bash
git clone https://github.com/ArchanaChetan07/InferSight && cd InferSight
docker compose up
```

- Mock vLLM: `:8000` · Sidecar: `:8020` · Prometheus: `:9090` · Grafana: `:3000` (dashboard pre-provisioned)

If those host ports are already taken, override them:

```bash
MOCK_PORT=18080 SIDECAR_PORT=18020 PROM_PORT=19091 GRAFANA_PORT=13001 HOSTED_PORT=19000 \
  docker compose up
```

## How it works

```
client ──► InferSight sidecar :8020 ──► vLLM :8000
                 │
                 ├── SSE chunks timestamped out-of-band (2 clock reads/chunk)
                 ├── /metrics  → Prometheus / Grafana        (OSS, free)
                 └── batched ship → hosted dashboard + alerts (paid, optional)
```

The sidecar passes streamed bytes through untouched and records timing out-of-band, so overhead on the token hot path is microseconds. It also scrapes vLLM's own `/metrics` to surface KV-cache pressure and queue depth alongside request-level timing. Exact token counts come from `stream_options.include_usage`, which the sidecar injects automatically.

## Hosted tier (optional)

Don't want to run Prometheus + Grafana? Ship metrics to a hosted dashboard instead:

```bash
infersight run --upstream http://localhost:8000 \
  --hosted-api-key isk_your_key
```

You get:
- A zero-ops dashboard (TTFT/TBT/E2E percentiles, throughput, error rate)
- **LLM-aware alerting** to Slack/PagerDuty — post-deploy P99 regressions, KV-cache trending toward OOM, error-rate spikes — with per-rule cooldowns so an incident is one page, not forty
- **Multi-model & multi-cluster comparison**, so "is the new model version actually faster in prod?" is a table, not an investigation

Self-hosting the hosted tier is also supported (it's in this repo — `hosted/`):

```bash
uvicorn hosted.ingest:app --port 9000
# create a tenant:
curl -X POST localhost:9000/v1/admin/tenants \
  -H "Authorization: Bearer $INFERSIGHT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"my-team"}'
```

## Configuration

CLI flags > `INFERSIGHT_*` env vars > `--config infersight.json` > defaults.

```bash
INFERSIGHT_UPSTREAM_URL=http://vllm:8000
INFERSIGHT_LISTEN_PORT=8020
INFERSIGHT_HOSTED__API_KEY=isk_...      # double underscore for nested keys
```

See [docs/configuration.md](docs/configuration.md) for the full reference, [docs/architecture.md](docs/architecture.md) for design notes, and [docs/limitations.md](docs/limitations.md) for known limitations.

## Engine support

| Engine | Status |
| --- | --- |
| vLLM | ✅ full (request timing + KV cache + queue depth) |
| Any OpenAI-compatible server | ✅ request timing |
| TGI, SGLang engine gauges | 🛠 planned — contributions welcome |

## Development

```bash
pip install -e ".[dev]"
pytest          # 32 tests: unit, hosted-tier, regression, and live end-to-end
```

Apache-2.0. Built by [Archana Suresh Patil](mailto:apatil@sandiego.edu) — feedback and design partners welcome.
