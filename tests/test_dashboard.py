# coding: utf-8
"""Offline tests for the dashboard aggregates (core/dashboard.py).
No /proc, no openssl, no panel — all boundaries (list_apps, metrics, dir size,
jobs, SSL marker) are monkeypatched. Proves the aggregates are correct AND that
expiry comes from the cheap marker, never an openssl/sitestatus call."""
import datetime

import pytest

from core import dashboard


def _iso_in(days):
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _stub_common(monkeypatch, apps, metrics=None):
    monkeypatch.setattr(dashboard.instance, "list_apps", lambda: apps)
    monkeypatch.setattr(dashboard.instance, "metrics_all",
                        lambda names: {n: (metrics or {}).get(n, {"cpu_pct": None, "rss_mb": None}) for n in names})
    monkeypatch.setattr(dashboard.maintenance, "_dir_size", lambda p, ttl=0: 0)
    monkeypatch.setattr(dashboard.jobs, "list_jobs", lambda limit=8: [])
    monkeypatch.setattr(dashboard.ssl, "read_ssl_not_after", lambda app: None)


def test_app_counts(monkeypatch):
    apps = [
        {"app": "a", "status": "active", "runtime_ok": True, "ssl": False},
        {"app": "b", "status": "inactive", "runtime_ok": True, "ssl": False},
        {"app": "c", "status": "active", "runtime_ok": False, "ssl": False},  # running but JDK gone
    ]
    _stub_common(monkeypatch, apps)
    s = dashboard.summary()
    assert s["apps"] == {"total": 3, "running": 2, "down": 1, "runtime_missing": 1}


def test_resources_sum_only_running(monkeypatch):
    apps = [
        {"app": "a", "status": "active", "runtime_ok": True},
        {"app": "b", "status": "active", "runtime_ok": True},
        {"app": "c", "status": "inactive", "runtime_ok": True},  # not sampled
    ]
    metrics = {
        "a": {"cpu_pct": 12.5, "rss_mb": 100.0},
        "b": {"cpu_pct": 7.5, "rss_mb": 50.0},
    }
    _stub_common(monkeypatch, apps, metrics)
    s = dashboard.summary()
    assert s["resources"]["sampled"] == 2
    assert s["resources"]["cpu_pct_total"] == 20.0
    assert s["resources"]["rss_mb_total"] == 150.0


def test_ssl_expiring_from_marker(monkeypatch):
    apps = [
        {"app": "soon", "status": "active", "runtime_ok": True, "ssl": True, "domain": "soon.example.com"},
        {"app": "later", "status": "active", "runtime_ok": True, "ssl": True, "domain": "later.example.com"},
        {"app": "plain", "status": "active", "runtime_ok": True, "ssl": False, "domain": None},
    ]
    _stub_common(monkeypatch, apps)
    markers = {"soon": _iso_in(10), "later": _iso_in(200)}
    monkeypatch.setattr(dashboard.ssl, "read_ssl_not_after", lambda app: markers.get(app))
    s = dashboard.summary()
    assert s["ssl"]["with_ssl"] == 2
    assert s["ssl"]["expiring_soon"] == 1
    assert s["ssl"]["expiring"][0]["app"] == "soon"
    assert 0 <= s["ssl"]["expiring"][0]["days_left"] < dashboard.EXPIRY_WARN_DAYS


def test_no_openssl_or_sitestatus_call(monkeypatch):
    """Dashboard must compute expiry from the marker only — never the heavy
    sitestatus/openssl path. If it tried, this would blow up the summary."""
    apps = [{"app": "x", "status": "active", "runtime_ok": True, "ssl": True, "domain": "x.example.com"}]
    _stub_common(monkeypatch, apps)
    monkeypatch.setattr(dashboard.ssl, "read_ssl_not_after", lambda app: _iso_in(5))
    from core.deploy import sitestatus
    def _boom(*a, **k):
        raise AssertionError("sitestatus._cert_info must not be called from the dashboard")
    monkeypatch.setattr(sitestatus, "_cert_info", _boom)
    s = dashboard.summary()  # must not raise
    assert s["ssl"]["expiring_soon"] == 1


def test_malformed_is_tolerated(monkeypatch):
    def _boom():
        raise RuntimeError("instance store is broken")
    monkeypatch.setattr(dashboard.instance, "list_apps", _boom)
    monkeypatch.setattr(dashboard.maintenance, "_dir_size", lambda p: 0)
    monkeypatch.setattr(dashboard.jobs, "list_jobs", lambda limit=8: [])
    s = dashboard.summary()  # never raises
    assert s["apps"]["total"] == 0


def test_shape(monkeypatch):
    _stub_common(monkeypatch, [])
    s = dashboard.summary()
    for key in ("apps", "resources", "ssl", "disk", "recent_tasks"):
        assert key in s
    for key in ("total", "running", "down", "runtime_missing"):
        assert key in s["apps"]
    for key in ("instances_mb", "backups_mb"):
        assert key in s["disk"]
