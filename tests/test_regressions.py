"""Regression tests for bugs found in the audit."""

import asyncio
import time

import pytest

from infersight.config import InferSightConfig
from infersight.forwarder import HostedForwarder
from infersight.metrics import RequestRecord
from infersight.proxy import SSELineBuffer, _bounded_model_label, _seen_models
from hosted.storage import MetricStore


# ---------------------------------------------------------------------- #
# Bug 1: SSE lines split across network reads were silently dropped
# ---------------------------------------------------------------------- #


def test_sse_buffer_handles_split_lines():
    buf = SSELineBuffer()
    # A data line split mid-prefix and mid-payload across three reads.
    assert buf.feed(b"da") == []
    assert buf.feed(b'ta: {"tok":1}\nda') == [b'{"tok":1}']
    assert buf.feed(b'ta: {"tok":2}\n\n') == [b'{"tok":2}']


def test_sse_buffer_multiple_lines_one_read():
    buf = SSELineBuffer()
    lines = buf.feed(b'data: {"a":1}\n\ndata: {"b":2}\n\ndata: [DONE]\n\n')
    assert lines == [b'{"a":1}', b'{"b":2}']  # [DONE] and blanks excluded


def test_sse_buffer_split_usage_payload():
    # The '"usage"' final chunk split across reads must still be reassembled.
    buf = SSELineBuffer()
    assert buf.feed(b'data: {"usage": {"prompt_t') == []
    lines = buf.feed(b'okens": 5, "completion_tokens": 7}}\n')
    assert lines == [b'{"usage": {"prompt_tokens": 5, "completion_tokens": 7}}']


# ---------------------------------------------------------------------- #
# Bug 2: unbounded model-label cardinality from untrusted payloads
# ---------------------------------------------------------------------- #


def test_model_label_cardinality_capped():
    _seen_models.clear()
    cfg = InferSightConfig(max_model_labels=3)
    assert _bounded_model_label("a", cfg) == "a"
    assert _bounded_model_label("b", cfg) == "b"
    assert _bounded_model_label("c", cfg) == "c"
    assert _bounded_model_label("d", cfg) == "__other__"   # over the cap
    assert _bounded_model_label("a", cfg) == "a"            # known ones still pass
    _seen_models.clear()


# ---------------------------------------------------------------------- #
# Bug 3: final flush sent one unbounded batch that the ingest cap rejects
# ---------------------------------------------------------------------- #


def test_final_flush_respects_batch_cap():
    sent_batches: list[int] = []

    cfg = InferSightConfig(hosted={
        "enabled": True, "ingest_url": "http://test/v1/ingest",
        "api_key": "k", "max_batch_size": 10,
    })
    fwd = HostedForwarder(cfg)

    async def fake_send(batch):
        sent_batches.append(len(batch))
        return True

    fwd._send = fake_send  # type: ignore[method-assign]
    for i in range(35):
        fwd.enqueue(RequestRecord(
            ts=time.time(), model="m", endpoint="/v1/completions", engine="vllm",
            status="200", e2e_seconds=1.0,
        ))
    sent = asyncio.run(fwd.flush(final=True))
    assert sent == 35
    assert sent_batches == [10, 10, 10, 5]  # capped slices, not one giant POST


def test_steady_state_flush_sends_one_batch():
    cfg = InferSightConfig(hosted={"enabled": True, "api_key": "k", "max_batch_size": 10})
    fwd = HostedForwarder(cfg)

    async def fake_send(batch):
        return True

    fwd._send = fake_send  # type: ignore[method-assign]
    for _ in range(25):
        fwd.enqueue(RequestRecord(
            ts=time.time(), model="m", endpoint="/v1/completions", engine="vllm",
            status="200", e2e_seconds=1.0,
        ))
    assert asyncio.run(fwd.flush()) == 10
    assert len(fwd._queue) == 15


# ---------------------------------------------------------------------- #
# Bug 4: one malformed record 500'd the whole ingest batch (data loss)
# ---------------------------------------------------------------------- #


def test_malformed_record_does_not_poison_batch(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    t = store.create_tenant("t")
    good = {"ts": time.time(), "model": "m", "endpoint": "/v1/completions",
            "engine": "vllm", "status": "200", "e2e_seconds": 1.0}
    bad = {"ts": "not-a-number", "e2e_seconds": {"nested": "garbage"}}
    accepted, rejected = store.insert_records(t.id, [good, bad, good])
    assert (accepted, rejected) == (2, 1)
    assert store.summary(t.id)["requests"] == 2


# ---------------------------------------------------------------------- #
# Bug 5: no retention — records table grew forever
# ---------------------------------------------------------------------- #


def test_prune_removes_only_expired(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    t = store.create_tenant("t")
    now = time.time()
    old = {"ts": now - 100_000, "model": "m", "endpoint": "e", "engine": "vllm",
           "status": "200", "e2e_seconds": 1.0}
    fresh = dict(old, ts=now)
    store.insert_records(t.id, [old, old, fresh])
    removed = store.prune(retention_seconds=86_400)
    assert removed == 2
    assert store.summary(t.id, since_seconds=200_000)["requests"] == 1


# ---------------------------------------------------------------------- #
# Bug 6: usage injection is now opt-out for servers that reject it
# ---------------------------------------------------------------------- #


def test_inject_stream_usage_configurable():
    assert InferSightConfig().inject_stream_usage is True
    assert InferSightConfig(inject_stream_usage=False).inject_stream_usage is False
