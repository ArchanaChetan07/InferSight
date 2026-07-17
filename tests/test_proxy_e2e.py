"""End-to-end: real mock-vLLM uvicorn server behind a real InferSight proxy."""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait(url: str, proc: subprocess.Popen | None = None, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            stderr = ""
            if proc.stderr:
                stderr = proc.stderr.read().decode(errors="replace")
            raise RuntimeError(
                f"server process for {url} exited early (code={proc.returncode})\n{stderr}"
            )
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError:
            time.sleep(0.15)
    raise TimeoutError(f"server at {url} never came up")


@pytest.fixture(scope="module")
def stack():
    up_port, proxy_port = _free_port(), _free_port()
    upstream = subprocess.Popen(
        [sys.executable, "examples/mock_vllm.py", "--port", str(up_port), "--ttft", "0.05", "--tbt", "0.008"],
        cwd=ROOT,
        stderr=subprocess.PIPE,
    )
    proxy = subprocess.Popen(
        [sys.executable, "-m", "infersight.cli", "run",
         "--upstream", f"http://127.0.0.1:{up_port}", "--host", "127.0.0.1", "--port", str(proxy_port)],
        cwd=ROOT,
        stderr=subprocess.PIPE,
    )
    try:
        _wait(f"http://127.0.0.1:{up_port}/v1/models", upstream)
        _wait(f"http://127.0.0.1:{proxy_port}/infersight/health", proxy)
        yield f"http://127.0.0.1:{proxy_port}"
    finally:
        proxy.terminate()
        upstream.terminate()
        proxy.wait(timeout=10)
        upstream.wait(timeout=10)


def test_health(stack):
    r = httpx.get(f"{stack}/infersight/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_passthrough_models(stack):
    r = httpx.get(f"{stack}/v1/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"].startswith("meta-llama")


def test_non_streaming_completion_metrics(stack):
    r = httpx.post(f"{stack}/v1/chat/completions", json={
        "model": "meta-llama/Llama-3.1-8B-Instruct",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }, timeout=30)
    assert r.status_code == 200
    assert r.json()["usage"]["completion_tokens"] == 16

    m = httpx.get(f"{stack}/metrics").text
    assert "infersight_requests_total" in m
    assert 'status="200"' in m
    assert "infersight_completion_tokens_total" in m


def test_streaming_ttft_tbt_and_usage(stack):
    ttft_wall = None
    chunks = 0
    start = time.perf_counter()
    with httpx.stream("POST", f"{stack}/v1/chat/completions", json={
        "model": "meta-llama/Llama-3.1-8B-Instruct",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 24, "stream": True,
    }, timeout=30) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:") and "[DONE]" not in line:
                if ttft_wall is None:
                    ttft_wall = time.perf_counter() - start
                chunks += 1
    assert chunks >= 24  # 24 content chunks (+ usage chunk injected by sidecar)
    assert ttft_wall is not None and ttft_wall >= 0.02

    time.sleep(0.3)  # let the finally block record
    m = httpx.get(f"{stack}/metrics").text
    assert "infersight_ttft_seconds_bucket" in m
    assert "infersight_tbt_seconds_count" in m
    # Exact token counts came from injected stream_options include_usage.
    assert 'infersight_completion_tokens_total{endpoint="/v1/chat/completions"' in m


def test_engine_gauges_scraped(stack):
    time.sleep(1.0)  # first scrape happens immediately on startup
    m = httpx.get(f"{stack}/metrics").text
    assert "infersight_kv_cache_usage_ratio" in m
    assert 'infersight_queue_depth{engine="vllm"' in m


def test_upstream_error_is_502():
    # Proxy pointed at a dead upstream must fail fast and cleanly.
    port = _free_port()
    proxy = subprocess.Popen(
        [sys.executable, "-m", "infersight.cli", "run",
         "--upstream", "http://127.0.0.1:1", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        stderr=subprocess.PIPE,
    )
    try:
        _wait(f"http://127.0.0.1:{port}/infersight/health", proxy)
        r = httpx.post(f"http://127.0.0.1:{port}/v1/completions", json={"model": "x", "prompt": "y"}, timeout=15)
        assert r.status_code == 502
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)
