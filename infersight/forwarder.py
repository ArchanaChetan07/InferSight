"""Ships request records to the hosted InferSight tier.

Design constraints:
  * Never block or slow the inference hot path — enqueue is O(1), lossy
    under extreme backlog (drop-oldest), and all I/O happens in a
    background task.
  * Batched, with a bounded queue and interval flushing.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import httpx

from infersight.config import InferSightConfig
from infersight.metrics import RequestRecord

log = logging.getLogger("infersight.forwarder")

MAX_QUEUE = 10_000


class HostedForwarder:
    def __init__(self, config: InferSightConfig) -> None:
        self.config = config
        self._queue: deque[RequestRecord] = deque(maxlen=MAX_QUEUE)
        self._client = httpx.AsyncClient(timeout=10.0)

    def enqueue(self, rec: RequestRecord) -> None:
        self._queue.append(rec)  # drop-oldest via maxlen under backlog

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.config.hosted.flush_interval_seconds)
                await self.flush()
        except asyncio.CancelledError:
            return

    async def flush(self, final: bool = False) -> int:
        """Send queued records in capped batches. Returns count sent.

        On shutdown (final=True) the whole backlog is drained, but still in
        max_batch_size slices — a single oversized request would be rejected
        by the ingest endpoint's batch cap and lose everything.
        """
        sent = 0
        while self._queue:
            batch: list[RequestRecord] = []
            while self._queue and len(batch) < self.config.hosted.max_batch_size:
                batch.append(self._queue.popleft())
            if not await self._send(batch):
                break
            sent += len(batch)
            if not final:
                break  # steady state: one batch per flush interval
        return sent

    async def _send(self, batch: list[RequestRecord]) -> bool:
        try:
            resp = await self._client.post(
                self.config.hosted.ingest_url,
                json={"records": [r.to_dict() for r in batch]},
                headers={"Authorization": f"Bearer {self.config.hosted.api_key}"},
            )
        except httpx.HTTPError as exc:
            self._requeue(batch)
            log.warning("hosted ingest unreachable: %s", exc)
            return False
        if 500 <= resp.status_code < 600:
            self._requeue(batch)  # server hiccup — retry next interval
            log.warning("hosted ingest 5xx: %s", resp.status_code)
            return False
        if resp.status_code >= 400:
            # 4xx (bad key, oversized, malformed) will not succeed on retry;
            # drop rather than loop forever.
            log.warning("hosted ingest rejected batch: %s %s", resp.status_code, resp.text[:200])
            return False
        return True

    def _requeue(self, batch: list[RequestRecord]) -> None:
        for rec in reversed(batch):
            self._queue.appendleft(rec)

    async def send_engine_state(self, **snapshot: object) -> None:
        """Ship a KV-cache/queue snapshot; best-effort, never raises."""
        url = self.config.hosted.ingest_url.rstrip("/")
        if url.endswith("/ingest"):
            url += "/engine-state"
        try:
            await self._client.post(
                url,
                json=snapshot,
                headers={"Authorization": f"Bearer {self.config.hosted.api_key}"},
            )
        except httpx.HTTPError as exc:
            log.debug("engine-state ship failed: %s", exc)

    async def aclose(self) -> None:
        await self._client.aclose()
