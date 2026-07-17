# vLLM community post (Discourse / Discord)

**Title:** Built an observability sidecar for vLLM (TTFT, TBT, KV-cache) — looking for 3–5 design partners

Hey all — I've been running vLLM in production and kept rebuilding the same
Prometheus exporters and Grafana panels for every deployment. I've packaged
that work into an open-source sidecar, and before I launch it more widely I'd
love a handful of teams to try it and tell me what's missing.

What it does today:
- transparent proxy in front of vLLM (streaming supported, no engine changes)
- TTFT / TBT / E2E histograms per model, exact token counts
- KV-cache usage + queue depth pulled from vllm:/metrics
- ready-to-import Grafana dashboard, 5-minute install (pip or Docker)

What I'm asking of design partners: run it next to one real deployment for a
couple of weeks, and a 30-minute call about what you'd need before you'd rely
on it. In return you get direct influence on the roadmap (TGI/SGLang gauges,
multi-replica views, alerting rules) and free access to the hosted tier when
it opens.

DM me or reply here if interested. Repo + demo stack: <link>
