"""Configuration for the InferSight sidecar.

Everything is config-driven (Phase 1 requirement). Precedence, highest first:

    1. Explicit CLI flags        (infersight run --upstream ...)
    2. Environment variables     (INFERSIGHT_*)
    3. Config file               (--config infersight.json)
    4. Built-in defaults

Only stdlib + pydantic are used so the sidecar stays lightweight.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

ENV_PREFIX = "INFERSIGHT_"


class HostedConfig(BaseModel):
    """Optional shipping of metrics to the hosted (paid) tier."""

    enabled: bool = False
    ingest_url: str = "https://ingest.infersight.dev/v1/ingest"
    api_key: str = ""
    flush_interval_seconds: float = 15.0
    # Batches are capped so a slow ingest endpoint can never back-pressure
    # the inference hot path.
    max_batch_size: int = 500


class InferSightConfig(BaseModel):
    """Top-level sidecar configuration."""

    # `model_label` is intentional; silence pydantic's protected `model_` namespace.
    model_config = ConfigDict(protected_namespaces=())

    # --- proxy ---
    listen_host: str = "0.0.0.0"
    listen_port: int = 8020
    upstream_url: str = "http://localhost:8000"  # the vLLM server
    request_timeout_seconds: float = 600.0

    # --- engine ---
    engine: str = "vllm"  # vllm | tgi | sglang | openai-compatible
    model_label: str = ""  # override; otherwise taken from request payload

    # Inject stream_options.include_usage into streaming requests for exact
    # token counts. vLLM supports this; disable for servers that reject
    # unknown fields. NOTE: when injected, clients receive one extra final
    # chunk with empty `choices` and a `usage` object (OpenAI-spec behavior).
    inject_stream_usage: bool = True

    # Cap on distinct `model` label values to prevent metric-cardinality
    # explosion from untrusted client payloads; overflow maps to "__other__".
    max_model_labels: int = 50

    # --- metrics ---
    metrics_path: str = "/metrics"
    # Histogram buckets tuned for LLM serving, in seconds.
    ttft_buckets: list[float] = Field(
        default_factory=lambda: [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
    )
    tbt_buckets: list[float] = Field(
        default_factory=lambda: [0.005, 0.01, 0.02, 0.04, 0.08, 0.15, 0.3, 0.6, 1.2]
    )
    e2e_buckets: list[float] = Field(
        default_factory=lambda: [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0]
    )

    # --- vLLM engine metric scraping (KV cache, queue depth) ---
    scrape_engine_metrics: bool = True
    engine_metrics_interval_seconds: float = 5.0

    # --- auto-discovery ---
    discovery_hosts: list[str] = Field(default_factory=lambda: ["localhost"])
    discovery_ports: list[int] = Field(default_factory=lambda: [8000, 8001, 8002, 8080])

    # --- hosted tier ---
    hosted: HostedConfig = Field(default_factory=HostedConfig)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        config_file: Optional[str] = None,
        overrides: Optional[dict[str, Any]] = None,
    ) -> "InferSightConfig":
        """Merge file -> env -> explicit overrides into a config object."""
        data: dict[str, Any] = {}

        if config_file:
            path = Path(config_file)
            if not path.exists():
                raise FileNotFoundError(f"Config file not found: {config_file}")
            data.update(json.loads(path.read_text()))

        data = _deep_merge(data, _from_env())

        if overrides:
            data = _deep_merge(data, {k: v for k, v in overrides.items() if v is not None})

        return cls.model_validate(data)


def _from_env() -> dict[str, Any]:
    """Map INFERSIGHT_* env vars onto config fields.

    Nested fields use double underscores: INFERSIGHT_HOSTED__API_KEY.
    """
    out: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        value: Any = raw
        # Light coercion: JSON first (handles ints, floats, bools, lists),
        # falling back to the raw string.
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return out


def _deep_merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged
