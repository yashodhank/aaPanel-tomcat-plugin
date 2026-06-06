# coding: utf-8
"""
Operational dashboard aggregates (read-only).

WHY a separate module/endpoint from GetStatus: GetStatus is the fast ~5s poll
that feeds the install-count tiles; it must stay cheap. The aggregates here are
heavier — per-app CPU sampling (instance.metrics sleeps ~0.12s/app), recursive
directory sizing, cert-expiry parsing — so they live behind GetDashboard, which
the UI loads only on dashboard-tab activation / manual refresh.

Everything is DEFENSIVE: each sub-aggregate is wrapped so one malformed app (or a
process that exits mid-sample) never breaks the whole panel. All numbers are
best-effort; an unavailable value is None/0, never an exception.
"""
from __future__ import annotations

import datetime
import os
from typing import Dict, List, Optional

from . import jobs, maintenance
from .deploy import ssl
from .tomcat import instance

# Flag certs with fewer than this many days left.
EXPIRY_WARN_DAYS = 30
# Backups dir (Phase-2 store writes here; sized defensively even before it exists).
BACKUPS_ROOT = os.path.join(maintenance.DATA_ROOT, "backups")


def _days_left(iso: str) -> Optional[int]:
    """Whole days until an ISO-8601 UTC timestamp (negative if already past)."""
    try:
        dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return int((dt - now).total_seconds() // 86400)
    except Exception:
        return None


def _resources(running: List[Dict]) -> Dict:
    """Sum CPU% + RSS across running apps. Uses instance.metrics_all — ONE shared
    0.12s CPU-sample window + batched PID resolution — so this is O(0.12s) total
    regardless of how many apps are running (was ~0.12s per capped-thread wave)."""
    out = {"cpu_pct_total": 0.0, "rss_mb_total": 0.0, "sampled": 0}
    names = [a.get("app") for a in running if a.get("app")]
    if not names:
        return out
    try:
        metrics = instance.metrics_all(names)
    except Exception:
        metrics = {}
    cpu = rss = 0.0
    sampled = 0
    for n in names:
        m = metrics.get(n)
        if not m:
            continue
        sampled += 1
        if m.get("cpu_pct") is not None:
            cpu += m["cpu_pct"]
        if m.get("rss_mb") is not None:
            rss += m["rss_mb"]
    out["cpu_pct_total"] = round(cpu, 1)
    out["rss_mb_total"] = round(rss, 1)
    out["sampled"] = sampled
    return out


def _ssl_summary(apps: List[Dict]) -> Dict:
    """Count SSL-enabled apps and flag those whose cert expires within
    EXPIRY_WARN_DAYS. Expiry is read from the per-app marker body (cheap; no
    openssl/network). Apps with a legacy "1" marker contribute to with_ssl but
    can't be expiry-checked here (left out of `expiring`)."""
    with_ssl = 0
    expiring: List[Dict] = []
    for a in apps:
        try:
            if not a.get("ssl"):
                continue
            with_ssl += 1
            app = a.get("app")
            not_after = ssl.read_ssl_not_after(app)
            if not not_after:
                continue
            days = _days_left(not_after)
            if days is not None and days < EXPIRY_WARN_DAYS:
                expiring.append({
                    "app": app,
                    "domain": a.get("domain"),
                    "not_after": not_after,
                    "days_left": days,
                })
        except Exception:
            continue
    expiring.sort(key=lambda e: (e.get("days_left") is None, e.get("days_left")))
    return {"with_ssl": with_ssl, "expiring_soon": len(expiring), "expiring": expiring}


def summary() -> Dict:
    """Aggregate operational snapshot for the Dashboard. Never raises."""
    try:
        apps = instance.list_apps()
    except Exception:
        apps = []

    running = [a for a in apps if a.get("status") == "active"]
    total = len(apps)
    apps_block = {
        "total": total,
        "running": len(running),
        "down": total - len(running),
        "runtime_missing": sum(1 for a in apps if a.get("runtime_ok") is False),
    }

    try:
        resources = _resources(running)
    except Exception:
        resources = {"cpu_pct_total": 0.0, "rss_mb_total": 0.0, "sampled": 0}

    try:
        ssl_block = _ssl_summary(apps)
    except Exception:
        ssl_block = {"with_ssl": 0, "expiring_soon": 0, "expiring": []}

    try:
        inst_bytes = maintenance._dir_size(instance.INSTANCE_ROOT)
        bak_bytes = maintenance._dir_size(BACKUPS_ROOT)
        disk = {
            "instances_bytes": inst_bytes,
            "instances_mb": round(inst_bytes / (1 << 20), 1),
            "backups_bytes": bak_bytes,
            "backups_mb": round(bak_bytes / (1 << 20), 1),
        }
    except Exception:
        disk = {"instances_bytes": 0, "instances_mb": 0.0,
                "backups_bytes": 0, "backups_mb": 0.0}

    try:
        recent_tasks = jobs.list_jobs(limit=8)
    except Exception:
        recent_tasks = []

    return {
        "apps": apps_block,
        "resources": resources,
        "ssl": ssl_block,
        "disk": disk,
        "recent_tasks": recent_tasks,
    }
