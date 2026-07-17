# Show HN draft

**Title:** Show HN: InferSight – TTFT/TBT/KV-cache observability for self-hosted vLLM

**Post:**

I run LLMs on self-hosted vLLM, and every latency regression I've had was
invisible to normal monitoring. CPU fine, memory fine, 200s everywhere — and
time-to-first-token had quietly doubled after a deploy.

So I built InferSight: a small sidecar proxy you point at vLLM (or any
OpenAI-compatible server). It passes traffic through untouched and measures
the things that actually describe LLM serving quality:

- TTFT and time-between-tokens histograms, per model
- exact token throughput (it injects stream_options.include_usage)
- KV-cache usage and scheduler queue depth, scraped from vLLM's own /metrics
- P50–P99 for everything, in import-ready Grafana dashboards

Install is `pip install infersight`, then
`infersight run --upstream http://localhost:8000`. There's a docker-compose
demo with a mock vLLM so you can try it without a GPU.

Technical bits I sweated: timing happens out-of-band from the byte stream
(two perf_counter reads per chunk, no parsing on the hot path), and the
optional hosted shipping is fail-open with a bounded drop-oldest queue so
telemetry can never back-pressure inference.

The core is Apache-2.0. There's an optional hosted tier (dashboard + Slack/
PagerDuty alerts for P99 regressions and KV-cache pressure) — that's the
eventual business, but the OSS sidecar is complete on its own.

I'd especially love feedback from anyone running vLLM in production: what
signals do you wish you had? Repo: <link>
