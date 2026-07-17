"""Hosted tier tests: multi-tenant isolation, ingest auth, alert rules."""

import time

import pytest
from fastapi.testclient import TestClient

from hosted.alerting import AlertEngine, AlertRuleConfig
from hosted.storage import MetricStore


def _rec(ts=None, model="llama-3", status="200", e2e=1.0, ttft=0.1, ctoks=30):
    return {
        "ts": ts or time.time(), "model": model, "endpoint": "/v1/chat/completions",
        "engine": "vllm", "status": status, "e2e_seconds": e2e, "ttft_seconds": ttft,
        "tbt_seconds_mean": 0.02, "prompt_tokens": 100, "completion_tokens": ctoks,
        "streamed": True,
    }


@pytest.fixture()
def store(tmp_path):
    return MetricStore(str(tmp_path / "test.db"))


def test_tenant_isolation(store):
    a = store.create_tenant("team-a")
    b = store.create_tenant("team-b")
    store.insert_records(a.id, [_rec() for _ in range(5)])
    store.insert_records(b.id, [_rec(model="mistral") for _ in range(2)])
    assert store.summary(a.id)["requests"] == 5
    assert store.summary(b.id)["requests"] == 2
    assert store.models(a.id) == ["llama-3"]
    assert store.models(b.id) == ["mistral"]


def test_percentiles_and_comparison(store):
    t = store.create_tenant("t")
    store.insert_records(t.id, [_rec(ttft=0.1 * i or 0.01, model="fast") for i in range(1, 11)])
    store.insert_records(t.id, [_rec(ttft=0.5, model="slow") for _ in range(10)])
    p99_fast = store.percentile(t.id, "ttft_seconds", 0.99, 3600, model="fast")
    assert p99_fast == pytest.approx(1.0, rel=0.01)
    comp = store.model_comparison(t.id)
    by_model = {c["model"]: c for c in comp}
    assert by_model["slow"]["ttft_p99"] == pytest.approx(0.5)
    assert by_model["fast"]["requests"] == 10


def test_p99_regression_alert_fires(store):
    t = store.create_tenant("t")
    now = time.time()
    cfg = AlertRuleConfig(regression_window_seconds=300, baseline_window_seconds=600,
                          regression_threshold=0.5, min_requests=10)
    # Baseline: fast requests 5–15 minutes ago.
    store.insert_records(t.id, [_rec(ts=now - 400 - i, ttft=0.1, e2e=0.5) for i in range(40)])
    # Recent: slow requests within the last 5 minutes.
    store.insert_records(t.id, [_rec(ts=now - i, ttft=0.5, e2e=2.5) for i in range(40)])
    engine = AlertEngine(store, cfg)
    rules = {a.rule for a in engine.evaluate_tenant(t.id)}
    assert "p99_regression_ttft_seconds" in rules
    assert "p99_regression_e2e_seconds" in rules


def test_no_regression_alert_when_stable(store):
    t = store.create_tenant("t")
    now = time.time()
    cfg = AlertRuleConfig(regression_window_seconds=300, baseline_window_seconds=600, min_requests=10)
    store.insert_records(t.id, [_rec(ts=now - i * 10, ttft=0.1, e2e=0.5) for i in range(80)])
    engine = AlertEngine(store, cfg)
    assert not [a for a in engine.evaluate_tenant(t.id) if a.rule.startswith("p99_regression")]


def test_kv_cache_alert(store):
    t = store.create_tenant("t")
    engine = AlertEngine(store, AlertRuleConfig(kv_cache_alert_ratio=0.9))
    engine.update_engine_state(t.id, {"kv_cache_usage_ratio": 0.95, "queue_waiting": 7})
    alerts = engine.evaluate_tenant(t.id)
    assert any(a.rule == "kv_cache_pressure" for a in alerts)
    engine.update_engine_state(t.id, {"kv_cache_usage_ratio": 0.5})
    assert not any(a.rule == "kv_cache_pressure" for a in engine.evaluate_tenant(t.id))


def test_error_rate_alert(store):
    t = store.create_tenant("t")
    recs = [_rec(status="200") for _ in range(50)] + [_rec(status="500") for _ in range(10)]
    store.insert_records(t.id, recs)
    engine = AlertEngine(store, AlertRuleConfig(error_rate_threshold=0.05, min_requests=10))
    assert any(a.rule == "error_rate" for a in engine.evaluate_tenant(t.id))


# ---------------------------------------------------------------------- #
# API surface
# ---------------------------------------------------------------------- #


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("INFERSIGHT_HOSTED_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("INFERSIGHT_ADMIN_TOKEN", "admin_test")
    import importlib
    import hosted.ingest as ingest
    importlib.reload(ingest)
    return TestClient(ingest.app)


def test_ingest_requires_key(api):
    assert api.post("/v1/ingest", json={"records": []}).status_code == 401
    assert api.post("/v1/ingest", json={"records": []},
                    headers={"Authorization": "Bearer isk_bogus"}).status_code == 403


def test_full_api_flow(api):
    # Admin creates a tenant…
    r = api.post("/v1/admin/tenants", json={"name": "acme"},
                 headers={"Authorization": "Bearer admin_test"})
    assert r.status_code == 200
    key = r.json()["api_key"]
    auth = {"Authorization": f"Bearer {key}"}

    # …sidecar ships a batch…
    r = api.post("/v1/ingest", json={"records": [_rec() for _ in range(3)], "cluster": "us-east"}, headers=auth)
    assert r.status_code == 200 and r.json() == {"accepted": 3, "rejected": 0}

    # …dashboard reads it back.
    assert api.get("/v1/api/summary", headers=auth).json()["requests"] == 3
    models = api.get("/v1/api/models", headers=auth).json()["models"]
    assert models[0]["cluster"] == "us-east"
    assert api.get("/v1/api/timeseries", headers=auth).json()["series"]
    assert api.get("/v1/api/alerts", headers=auth).json() == {"alerts": []}

    # Dashboard UI is served.
    assert "InferSight" in api.get("/").text


def test_batch_size_limit(api):
    r = api.post("/v1/admin/tenants", json={"name": "big"},
                 headers={"Authorization": "Bearer admin_test"})
    key = r.json()["api_key"]
    r = api.post("/v1/ingest", json={"records": [_rec()] * 5001},
                 headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 413
