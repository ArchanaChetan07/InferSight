# Configuration reference

Precedence (highest wins): **CLI flags → `INFERSIGHT_*` env vars → `--config file.json` → defaults**.

Nested fields use `__` in env vars: `INFERSIGHT_HOSTED__API_KEY`.

## Sidecar

| Field | Env var | Default | Notes |
| --- | --- | --- | --- |
| `listen_host` | `INFERSIGHT_LISTEN_HOST` | `0.0.0.0` | |
| `listen_port` | `INFERSIGHT_LISTEN_PORT` | `8020` | |
| `upstream_url` | `INFERSIGHT_UPSTREAM_URL` | `http://localhost:8000` | your vLLM server |
| `engine` | `INFERSIGHT_ENGINE` | `vllm` | label: `vllm` \| `tgi` \| `sglang` \| `openai-compatible` |
| `model_label` | `INFERSIGHT_MODEL_LABEL` | *(from request)* | force a model label |
| `request_timeout_seconds` | `INFERSIGHT_REQUEST_TIMEOUT_SECONDS` | `600` | |
| `metrics_path` | `INFERSIGHT_METRICS_PATH` | `/metrics` | |
| `scrape_engine_metrics` | `INFERSIGHT_SCRAPE_ENGINE_METRICS` | `true` | scrape vLLM `/metrics` for KV cache + queue |
| `engine_metrics_interval_seconds` | `INFERSIGHT_ENGINE_METRICS_INTERVAL_SECONDS` | `5` | |
| `ttft_buckets` / `tbt_buckets` / `e2e_buckets` | *(config file)* | tuned for LLM serving | histogram buckets, seconds |
| `discovery_hosts` | `INFERSIGHT_DISCOVERY_HOSTS` | `["localhost"]` | JSON list in env |
| `discovery_ports` | `INFERSIGHT_DISCOVERY_PORTS` | `[8000,8001,8002,8080]` | |

## Hosted shipping (`hosted.*`)

| Field | Env var | Default |
| --- | --- | --- |
| `hosted.enabled` | `INFERSIGHT_HOSTED__ENABLED` | `false` |
| `hosted.ingest_url` | `INFERSIGHT_HOSTED__INGEST_URL` | `https://ingest.infersight.dev/v1/ingest` |
| `hosted.api_key` | `INFERSIGHT_HOSTED__API_KEY` | *(empty)* |
| `hosted.flush_interval_seconds` | `INFERSIGHT_HOSTED__FLUSH_INTERVAL_SECONDS` | `15` |
| `hosted.max_batch_size` | `INFERSIGHT_HOSTED__MAX_BATCH_SIZE` | `500` |

Shipping is fail-open: if the ingest endpoint is down, batches are requeued into a bounded buffer (10k records, drop-oldest) and inference traffic is never delayed.

## Hosted server (`hosted.ingest:app`)

| Env var | Purpose |
| --- | --- |
| `INFERSIGHT_HOSTED_DB` | SQLite path (default `infersight-hosted.db`) |
| `INFERSIGHT_ADMIN_TOKEN` | admin bearer token; auto-generated + printed if unset |
| `INFERSIGHT_SLACK_WEBHOOK` | Slack incoming-webhook URL for alerts |
| `INFERSIGHT_PAGERDUTY_KEY` | PagerDuty Events v2 routing key |

## Alert rules (defaults)

| Rule | Fires when | Severity |
| --- | --- | --- |
| `p99_regression_ttft_seconds` / `..._e2e_seconds` | last 10 min P99 > 1.5× the preceding 1 h baseline, ≥30 requests | critical |
| `kv_cache_pressure` | KV-cache usage ≥ 92% | critical |
| `error_rate` | non-2xx ≥ 5% over 10 min, ≥30 requests | warning |

Every rule has a 15-minute per-tenant cooldown.
