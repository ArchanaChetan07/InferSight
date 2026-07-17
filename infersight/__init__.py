"""InferSight — purpose-built observability for self-hosted LLM inference.

Open-source core: a lightweight sidecar/proxy for vLLM (and OpenAI-compatible
inference servers) that captures token-level serving metrics — TTFT, TBT,
end-to-end latency, throughput, and per-request token counts — and exposes
them as Prometheus metrics with optional shipping to a hosted ingest tier.
"""

__version__ = "0.1.0"

from infersight.config import InferSightConfig
from infersight.metrics import MetricsRegistry

__all__ = ["InferSightConfig", "MetricsRegistry", "__version__"]
