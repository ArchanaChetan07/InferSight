"""Auto-discovery of running vLLM / OpenAI-compatible inference servers.

Probes configured hosts and ports for:
  * GET /v1/models   → OpenAI-compatible server (model list)
  * GET /metrics     → engine identification (vLLM exposes `vllm:` metrics)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from infersight.config import InferSightConfig


@dataclass
class DiscoveredInstance:
    url: str
    engine: str  # "vllm" | "openai-compatible"
    models: list[str] = field(default_factory=list)


async def discover(config: InferSightConfig, timeout: float = 1.5) -> list[DiscoveredInstance]:
    targets = [
        f"http://{host}:{port}"
        for host in config.discovery_hosts
        for port in config.discovery_ports
    ]
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(*(_probe(client, url) for url in targets))
    return [r for r in results if r is not None]


async def _probe(client: httpx.AsyncClient, url: str) -> DiscoveredInstance | None:
    try:
        resp = await client.get(f"{url}/v1/models")
        if resp.status_code != 200:
            return None
        models = [m.get("id", "?") for m in resp.json().get("data", [])]
    except (httpx.HTTPError, ValueError):
        return None

    engine = "openai-compatible"
    try:
        metrics = await client.get(f"{url}/metrics")
        if metrics.status_code == 200 and "vllm:" in metrics.text:
            engine = "vllm"
    except httpx.HTTPError:
        pass

    return DiscoveredInstance(url=url, engine=engine, models=models)
