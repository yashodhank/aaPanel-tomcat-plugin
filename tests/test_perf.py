# coding: utf-8
"""Offline tests for the v0.19.0 batched hot-path helpers — the systemctl output
parsing is the main correctness risk. No real systemd/proc: shell.run + unit-file
existence are stubbed."""
import os

import pytest

from core.tomcat import instance, service


def _unit_exists_true(monkeypatch):
    orig = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: True if "javahost-" in str(p) else orig(p))


def test_status_all_parses_is_active(monkeypatch):
    monkeypatch.setattr(service, "have_systemd", lambda: True)
    _unit_exists_true(monkeypatch)
    monkeypatch.setattr(service.shell, "run", lambda argv, **k: (0, "active\ninactive\nactivating\n", ""))
    out = service.status_all(["a", "b", "c"])
    assert out == {"a": "active", "b": "inactive", "c": "activating"}


def test_status_all_count_mismatch_is_safe(monkeypatch):
    monkeypatch.setattr(service, "have_systemd", lambda: True)
    _unit_exists_true(monkeypatch)
    # 2 lines for 3 units -> refuse to guess, return {} (caller falls back per-app)
    monkeypatch.setattr(service.shell, "run", lambda argv, **k: (0, "active\nactive\n", ""))
    assert service.status_all(["a", "b", "c"]) == {}


def test_status_all_no_systemd_is_empty(monkeypatch):
    monkeypatch.setattr(service, "have_systemd", lambda: False)
    assert service.status_all(["a", "b"]) == {}


def test_resolve_pids_all_parses_show(monkeypatch):
    _unit_exists_true(monkeypatch)
    out_text = ("Id=javahost-a.service\nMainPID=123\n\n"
                "Id=javahost-b.service\nMainPID=0\n\n"
                "MainPID=456\nId=javahost-c.service\n")  # order-independent within a block
    monkeypatch.setattr(instance.shell, "run", lambda argv, **k: (0, out_text, ""))
    out = instance._resolve_pids_all(["a", "b", "c"])
    assert out["a"] == 123
    assert out["b"] is None        # MainPID=0 -> not running
    assert out["c"] == 456


def test_metrics_all_shape_no_pids(monkeypatch):
    # No systemd units, no pidfiles -> every app resolves to no PID, shape intact.
    monkeypatch.setattr(instance, "_resolve_pids_all", lambda names: {})
    monkeypatch.setattr(instance, "_resolve_pid", lambda a: None)
    out = instance.metrics_all(["x", "y"])
    assert set(out) == {"x", "y"}
    for n in ("x", "y"):
        assert out[n]["up"] is False and out[n]["cpu_pct"] is None
        assert set(out[n]) == {"app", "pid", "up", "rss_mb", "threads", "uptime_s", "cpu_pct"}
