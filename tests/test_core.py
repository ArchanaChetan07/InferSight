import json
import time

from infersight.config import InferSightConfig
from infersight.metrics import MetricsRegistry, RequestRecord, StreamTimer
from infersight.proxy import parse_engine_metrics


def test_config_defaults():
    cfg = InferSightConfig.load()
    assert cfg.listen_port == 8020
    assert cfg.upstream_url == "http://localhost:8000"
    assert cfg.hosted.enabled is False


def test_config_precedence(tmp_path, monkeypatch):
    f = tmp_path / "cfg.json"
    f.write_text(json.dumps({"listen_port": 9999, "engine": "tgi"}))
    monkeypatch.setenv("INFERSIGHT_LISTEN_PORT", "7777")
    monkeypatch.setenv("INFERSIGHT_HOSTED__API_KEY", "isk_test")
    cfg = InferSightConfig.load(config_file=str(f), overrides={"engine": "sglang"})
    assert cfg.listen_port == 7777        # env beats file
    assert cfg.engine == "sglang"         # override beats env/file
    assert cfg.hosted.api_key == "isk_test"  # nested env


def test_stream_timer_ttft_tbt():
    t = StreamTimer()
    time.sleep(0.03)
    assert t.on_chunk() is None           # first chunk → no gap
    time.sleep(0.02)
    gap = t.on_chunk()
    assert gap is not None and gap >= 0.015
    assert t.ttft is not None and t.ttft >= 0.025
    assert t.tbt_mean is not None and t.tbt_mean >= 0.015
    assert t.e2e >= t.ttft


def test_metrics_registry_observation():
    cfg = InferSightConfig()
    m = MetricsRegistry(cfg)
    m.observe_request(RequestRecord(
        ts=time.time(), model="llama-3", endpoint="/v1/chat/completions",
        engine="vllm", status="200", e2e_seconds=1.2, ttft_seconds=0.15,
        prompt_tokens=100, completion_tokens=40, streamed=True,
    ))
    m.observe_tbt(model="llama-3", endpoint="/v1/chat/completions", engine="vllm", gap_seconds=0.02)
    m.set_engine_gauges("http://localhost:8000", "vllm", 0.8, 3, 1, 0)
    text = m.render()[0].decode()
    assert 'infersight_requests_total{endpoint="/v1/chat/completions",engine="vllm",model="llama-3",status="200"} 1.0' in text
    assert "infersight_ttft_seconds_bucket" in text
    assert "infersight_tbt_seconds_bucket" in text
    assert 'infersight_kv_cache_usage_ratio{engine="vllm",upstream="http://localhost:8000"} 0.8' in text


def test_parse_engine_metrics():
    text = (
        '# HELP vllm:gpu_cache_usage_perc ...\n'
        'vllm:gpu_cache_usage_perc{model_name="m"} 0.42\n'
        'vllm:num_requests_running{model_name="m"} 5\n'
        'vllm:num_requests_waiting{model_name="m"} 2\n'
        'vllm:num_requests_swapped{model_name="m"} 0\n'
    )
    v = parse_engine_metrics(text)
    assert v == {"kv_cache": 0.42, "running": 5.0, "waiting": 2.0, "swapped": 0.0}
