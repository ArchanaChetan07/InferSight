"""Multi-tenant metric storage for the hosted tier.

SQLite for the MVP — one file, zero ops, easily swapped for Postgres/ClickHouse
behind the same interface once volume demands it. Tenancy is enforced at the
query layer: every read and write is scoped by tenant_id resolved from the
API key, never from client-supplied fields.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    api_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    engine TEXT NOT NULL,
    cluster TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,
    e2e_seconds REAL NOT NULL,
    ttft_seconds REAL,
    tbt_seconds_mean REAL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    streamed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_tenant_ts ON records(tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_records_tenant_model_ts ON records(tenant_id, model, ts);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    ts REAL NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Tenant:
    id: int
    name: str
    api_key: str


class MetricStore:
    def __init__(self, path: str = "infersight-hosted.db") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Tenancy
    # ------------------------------------------------------------------ #

    def create_tenant(self, name: str) -> Tenant:
        api_key = "isk_" + secrets.token_urlsafe(24)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tenants (name, api_key, created_at) VALUES (?, ?, ?)",
                (name, api_key, time.time()),
            )
            self._conn.commit()
        return Tenant(id=cur.lastrowid, name=name, api_key=api_key)

    def tenant_by_key(self, api_key: str) -> Optional[Tenant]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, api_key FROM tenants WHERE api_key = ?", (api_key,)
            ).fetchone()
        return Tenant(**dict(row)) if row else None

    def list_tenants(self) -> list[Tenant]:
        with self._lock:
            rows = self._conn.execute("SELECT id, name, api_key FROM tenants").fetchall()
        return [Tenant(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ #
    # Ingest
    # ------------------------------------------------------------------ #

    def insert_records(
        self, tenant_id: int, records: list[dict[str, Any]], cluster: str = "default"
    ) -> tuple[int, int]:
        """Insert records; malformed ones are skipped, not fatal.

        Returns (accepted, rejected). A batch mixing good and bad records
        must never lose the good ones to a single ValueError.
        """
        rows = []
        rejected = 0
        for r in records:
            try:
                rows.append((
                    tenant_id,
                    float(r.get("ts") or time.time()),
                    str(r.get("model") or "unknown")[:200],
                    str(r.get("endpoint") or "?")[:200],
                    str(r.get("engine") or "unknown")[:50],
                    cluster[:100],
                    str(r.get("status") or "?")[:50],
                    float(r.get("e2e_seconds") or 0.0),
                    _opt_float(r.get("ttft_seconds")),
                    _opt_float(r.get("tbt_seconds_mean")),
                    int(r.get("prompt_tokens") or 0),
                    int(r.get("completion_tokens") or 0),
                    1 if r.get("streamed") else 0,
                ))
            except (TypeError, ValueError, AttributeError):
                rejected += 1
        with self._lock:
            self._conn.executemany(
                """INSERT INTO records
                   (tenant_id, ts, model, endpoint, engine, cluster, status, e2e_seconds,
                    ttft_seconds, tbt_seconds_mean, prompt_tokens, completion_tokens, streamed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._conn.commit()
        return len(rows), rejected

    def prune(self, retention_seconds: float) -> int:
        """Delete records older than the retention window. Returns rows removed."""
        cutoff = time.time() - retention_seconds
        with self._lock:
            cur = self._conn.execute("DELETE FROM records WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM alert_events WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ #
    # Queries (all tenant-scoped)
    # ------------------------------------------------------------------ #

    def summary(self, tenant_id: int, since_seconds: float = 3600) -> dict:
        cutoff = time.time() - since_seconds
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS requests,
                          SUM(prompt_tokens) AS prompt_tokens,
                          SUM(completion_tokens) AS completion_tokens,
                          AVG(e2e_seconds) AS e2e_mean,
                          AVG(ttft_seconds) AS ttft_mean
                   FROM records WHERE tenant_id = ? AND ts >= ?""",
                (tenant_id, cutoff),
            ).fetchone()
            errors = self._conn.execute(
                """SELECT COUNT(*) FROM records
                   WHERE tenant_id = ? AND ts >= ? AND status NOT LIKE '2%'""",
                (tenant_id, cutoff),
            ).fetchone()[0]
        d = dict(row)
        total = d["requests"] or 0
        d["error_rate"] = (errors / total) if total else 0.0
        d["ttft_p99"] = self.percentile(tenant_id, "ttft_seconds", 0.99, since_seconds)
        d["e2e_p99"] = self.percentile(tenant_id, "e2e_seconds", 0.99, since_seconds)
        d["window_seconds"] = since_seconds
        return d

    def percentile(
        self,
        tenant_id: int,
        column: str,
        p: float,
        since_seconds: float,
        model: Optional[str] = None,
        cluster: Optional[str] = None,
        before_seconds: float = 0,
    ) -> Optional[float]:
        assert column in {"ttft_seconds", "e2e_seconds", "tbt_seconds_mean"}
        now = time.time()
        clauses = [f"tenant_id = ?", f"{column} IS NOT NULL", "ts >= ?", "ts <= ?"]
        params: list[Any] = [tenant_id, now - since_seconds - before_seconds, now - before_seconds]
        if model:
            clauses.append("model = ?")
            params.append(model)
        if cluster:
            clauses.append("cluster = ?")
            params.append(cluster)
        where = " AND ".join(clauses)
        with self._lock:
            count = self._conn.execute(
                f"SELECT COUNT(*) FROM records WHERE {where}", params
            ).fetchone()[0]
            if not count:
                return None
            offset = min(int(count * p), count - 1)
            row = self._conn.execute(
                f"SELECT {column} FROM records WHERE {where} ORDER BY {column} LIMIT 1 OFFSET ?",
                params + [offset],
            ).fetchone()
        return row[0] if row else None

    def timeseries(self, tenant_id: int, since_seconds: float = 3600, bucket_seconds: int = 60) -> list[dict]:
        cutoff = time.time() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT CAST(ts / ? AS INTEGER) * ? AS bucket,
                          COUNT(*) AS requests,
                          AVG(ttft_seconds) AS ttft_mean,
                          AVG(e2e_seconds) AS e2e_mean,
                          SUM(completion_tokens) AS completion_tokens
                   FROM records WHERE tenant_id = ? AND ts >= ?
                   GROUP BY bucket ORDER BY bucket""",
                (bucket_seconds, bucket_seconds, tenant_id, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def model_comparison(self, tenant_id: int, since_seconds: float = 3600) -> list[dict]:
        """Multi-model / multi-cluster comparison — the paid-tier view."""
        cutoff = time.time() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT model, cluster,
                          COUNT(*) AS requests,
                          AVG(ttft_seconds) AS ttft_mean,
                          AVG(e2e_seconds) AS e2e_mean,
                          AVG(tbt_seconds_mean) AS tbt_mean,
                          SUM(completion_tokens) AS completion_tokens
                   FROM records WHERE tenant_id = ? AND ts >= ?
                   GROUP BY model, cluster ORDER BY requests DESC""",
                (tenant_id, cutoff),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ttft_p99"] = self.percentile(tenant_id, "ttft_seconds", 0.99, since_seconds, model=d["model"], cluster=d["cluster"])
            d["e2e_p99"] = self.percentile(tenant_id, "e2e_seconds", 0.99, since_seconds, model=d["model"], cluster=d["cluster"])
            out.append(d)
        return out

    def models(self, tenant_id: int, since_seconds: float = 86400) -> list[str]:
        cutoff = time.time() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT model FROM records WHERE tenant_id = ? AND ts >= ?",
                (tenant_id, cutoff),
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Alert events
    # ------------------------------------------------------------------ #

    def record_alert(self, tenant_id: int, rule: str, severity: str, message: str, delivered: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO alert_events (tenant_id, ts, rule, severity, message, delivered) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, time.time(), rule, severity, message, 1 if delivered else 0),
            )
            self._conn.commit()

    def recent_alerts(self, tenant_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, rule, severity, message, delivered FROM alert_events WHERE tenant_id = ? ORDER BY ts DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_alert_ts(self, tenant_id: int, rule: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM alert_events WHERE tenant_id = ? AND rule = ?",
                (tenant_id, rule),
            ).fetchone()
        return row[0] or 0.0


def _opt_float(v: Any) -> Optional[float]:
    return None if v is None else float(v)
