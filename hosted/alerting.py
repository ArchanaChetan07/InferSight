"""LLM-aware alerting for the hosted tier.

Two launch rules, straight from the proposal:

  * p99_regression      — TTFT/E2E P99 in the recent window is >X% worse than
                          the preceding baseline window (catches bad deploys).
  * kv_cache_pressure   — KV-cache usage trending toward exhaustion/OOM.
                          (Fed by engine gauge snapshots shipped with batches.)
  * error_rate          — bonus rule: non-2xx ratio above threshold.

Delivery: Slack incoming webhook and PagerDuty Events API v2. Each rule has a
cooldown so a sustained incident produces one page, not a page storm.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from hosted.storage import MetricStore

log = logging.getLogger("infersight.alerting")


@dataclass
class AlertRuleConfig:
    # p99 regression
    regression_window_seconds: float = 600      # recent window
    baseline_window_seconds: float = 3600       # window immediately before it
    regression_threshold: float = 0.5           # alert if recent p99 > baseline * (1+0.5)
    min_requests: int = 30                      # don't alert on noise
    # kv cache
    kv_cache_alert_ratio: float = 0.92
    # error rate
    error_rate_threshold: float = 0.05
    # delivery
    cooldown_seconds: float = 900
    slack_webhook_url: str = ""
    pagerduty_routing_key: str = ""


@dataclass
class Alert:
    rule: str
    severity: str
    message: str


class AlertEngine:
    def __init__(self, store: MetricStore, config: Optional[AlertRuleConfig] = None) -> None:
        self.store = store
        self.config = config or AlertRuleConfig()
        # Latest engine gauge snapshot per tenant (kv cache etc.), fed by ingest.
        self._engine_state: dict[int, dict] = {}

    # ------------------------------------------------------------------ #
    # State fed from ingest
    # ------------------------------------------------------------------ #

    def update_engine_state(self, tenant_id: int, snapshot: dict) -> None:
        snapshot = dict(snapshot)
        snapshot["ts"] = time.time()
        self._engine_state[tenant_id] = snapshot

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def evaluate_tenant(self, tenant_id: int) -> list[Alert]:
        alerts: list[Alert] = []
        alerts += self._check_p99_regression(tenant_id)
        alerts += self._check_kv_cache(tenant_id)
        alerts += self._check_error_rate(tenant_id)
        return alerts

    def _check_p99_regression(self, tenant_id: int) -> list[Alert]:
        cfg = self.config
        out: list[Alert] = []
        for column, label in (("ttft_seconds", "TTFT"), ("e2e_seconds", "E2E latency")):
            recent = self.store.percentile(tenant_id, column, 0.99, cfg.regression_window_seconds)
            baseline = self.store.percentile(
                tenant_id, column, 0.99,
                cfg.baseline_window_seconds,
                before_seconds=cfg.regression_window_seconds,
            )
            if recent is None or baseline is None or baseline <= 0:
                continue
            summary = self.store.summary(tenant_id, cfg.regression_window_seconds)
            if (summary.get("requests") or 0) < cfg.min_requests:
                continue
            if recent > baseline * (1 + cfg.regression_threshold):
                pct = (recent / baseline - 1) * 100
                out.append(Alert(
                    rule=f"p99_regression_{column}",
                    severity="critical",
                    message=(
                        f"{label} P99 regression: {recent * 1000:.0f}ms in the last "
                        f"{cfg.regression_window_seconds / 60:.0f}m vs {baseline * 1000:.0f}ms baseline "
                        f"(+{pct:.0f}%). Did a deploy just land?"
                    ),
                ))
        return out

    def _check_kv_cache(self, tenant_id: int) -> list[Alert]:
        snap = self._engine_state.get(tenant_id)
        if not snap or time.time() - snap.get("ts", 0) > 300:
            return []
        ratio = snap.get("kv_cache_usage_ratio")
        if ratio is None:
            return []
        if ratio >= self.config.kv_cache_alert_ratio:
            waiting = snap.get("queue_waiting")
            extra = f" Queue waiting: {waiting:.0f}." if waiting is not None else ""
            return [Alert(
                rule="kv_cache_pressure",
                severity="critical",
                message=(
                    f"KV-cache usage at {ratio * 100:.0f}% and trending toward exhaustion — "
                    f"preemption/OOM risk.{extra} Consider raising gpu_memory_utilization, "
                    f"adding a replica, or lowering max concurrent sequences."
                ),
            )]
        return []

    def _check_error_rate(self, tenant_id: int) -> list[Alert]:
        summary = self.store.summary(tenant_id, self.config.regression_window_seconds)
        if (summary.get("requests") or 0) < self.config.min_requests:
            return []
        rate = summary.get("error_rate") or 0.0
        if rate >= self.config.error_rate_threshold:
            return [Alert(
                rule="error_rate",
                severity="warning",
                message=f"Error rate at {rate * 100:.1f}% over the last "
                        f"{self.config.regression_window_seconds / 60:.0f}m.",
            )]
        return []

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #

    async def deliver(self, tenant_id: int, tenant_name: str, alerts: list[Alert]) -> None:
        for alert in alerts:
            # Cooldown per (tenant, rule).
            if time.time() - self.store.last_alert_ts(tenant_id, alert.rule) < self.config.cooldown_seconds:
                continue
            delivered = False
            async with httpx.AsyncClient(timeout=10.0) as client:
                if self.config.slack_webhook_url:
                    delivered |= await self._send_slack(client, tenant_name, alert)
                if self.config.pagerduty_routing_key:
                    delivered |= await self._send_pagerduty(client, tenant_name, alert)
            self.store.record_alert(tenant_id, alert.rule, alert.severity, alert.message, delivered)
            log.info("alert [%s] %s: %s (delivered=%s)", alert.severity, alert.rule, alert.message, delivered)

    async def _send_slack(self, client: httpx.AsyncClient, tenant: str, alert: Alert) -> bool:
        emoji = ":rotating_light:" if alert.severity == "critical" else ":warning:"
        try:
            resp = await client.post(self.config.slack_webhook_url, json={
                "text": f"{emoji} *InferSight — {alert.rule}* ({tenant})\n{alert.message}"
            })
            return resp.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("Slack delivery failed: %s", exc)
            return False

    async def _send_pagerduty(self, client: httpx.AsyncClient, tenant: str, alert: Alert) -> bool:
        try:
            resp = await client.post("https://events.pagerduty.com/v2/enqueue", json={
                "routing_key": self.config.pagerduty_routing_key,
                "event_action": "trigger",
                "dedup_key": f"infersight-{tenant}-{alert.rule}",
                "payload": {
                    "summary": f"[InferSight] {alert.rule}: {alert.message}"[:1024],
                    "severity": alert.severity if alert.severity in {"critical", "warning", "error", "info"} else "warning",
                    "source": f"infersight/{tenant}",
                },
            })
            return resp.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("PagerDuty delivery failed: %s", exc)
            return False


async def evaluation_loop(engine: AlertEngine, interval_seconds: float = 60.0) -> None:
    """Background loop evaluating all tenants."""
    while True:
        try:
            for tenant in engine.store.list_tenants():
                alerts = engine.evaluate_tenant(tenant.id)
                if alerts:
                    await engine.deliver(tenant.id, tenant.name, alerts)
        except asyncio.CancelledError:
            return
        except Exception:  # keep the loop alive no matter what
            log.exception("alert evaluation failed")
        await asyncio.sleep(interval_seconds)
