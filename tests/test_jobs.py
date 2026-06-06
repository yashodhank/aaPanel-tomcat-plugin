# coding: utf-8
"""Offline tests for the background-job runner and reverse-proxy site endpoints.
No network, no panel, no nginx — JOBS_ROOT and VHOST_DIR are redirected to tmp."""
import os
import time

import pytest

from core import jobs
from core.deploy import proxy


# --------------------------------------------------------------------------- #
# job lifecycle
# --------------------------------------------------------------------------- #
def _wait_done(job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = jobs.read_log(job_id)
        if rec["state"] in ("done", "failed"):
            return rec
        time.sleep(0.05)
    return jobs.read_log(job_id)


def test_job_runs_to_done_and_captures_output(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    argv = [jobs.sys.executable or "python3", "-c", "print('hi-from-job')"]
    job_id = jobs.start("test", "t1", argv)

    # meta starts in running state
    metas = jobs.list_jobs()
    assert len(metas) == 1
    assert metas[0]["id"] == job_id
    assert metas[0]["state"] in ("running", "done")

    rec = _wait_done(job_id)
    assert rec["state"] == "done", rec
    assert "hi-from-job" in rec["log"]

    meta = jobs.list_jobs()[0]
    assert meta["state"] == "done"
    assert meta["ended"] is not None
    assert meta["started"] is not None
    assert meta["kind"] == "test"
    assert meta["target"] == "t1"


def test_failed_job_marked_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    argv = [jobs.sys.executable or "python3", "-c", "import sys; sys.exit(3)"]
    job_id = jobs.start("test", None, argv)
    rec = _wait_done(job_id)
    assert rec["state"] == "failed", rec
    assert "rc=3" in rec["message"]


def test_read_log_and_list_jobs_shapes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    job_id = jobs.start("test", "x", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(job_id)
    rec = jobs.read_log(job_id, lines=10)
    assert set(rec.keys()) == {"id", "state", "message", "log"}
    meta = jobs.list_jobs()[0]
    for k in ("id", "kind", "target", "state", "started", "ended", "message"):
        assert k in meta


def test_list_jobs_newest_first_and_tolerates_malformed(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    a = jobs.start("test", "a", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(a)
    time.sleep(0.02)
    b = jobs.start("test", "b", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(b)
    # a malformed dir (no meta.json) must be skipped, not crash
    os.makedirs(str(root / "garbage-dir"), exist_ok=True)
    metas = jobs.list_jobs()
    ids = [m["id"] for m in metas]
    assert ids[0] == b and ids[1] == a
    assert "garbage-dir" not in ids


def test_job_id_validation_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    for bad in ("../etc/passwd", "a/b", "..", "", "a b", "foo/../../x"):
        with pytest.raises(ValueError):
            jobs.job_dir(bad)
    with pytest.raises(ValueError):
        jobs.read_log("../../etc/passwd")


def test_python_work_builds_argv_with_plugin_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    argv = jobs.python_work("print('ok-from-work')")
    job_id = jobs.start("pywork", None, argv)
    rec = _wait_done(job_id)
    assert rec["state"] == "done", rec
    assert "ok-from-work" in rec["log"]


def test_prune_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    for i in range(4):
        jid = jobs.start("test", str(i), [jobs.sys.executable or "python3", "-c", "print(1)"])
        _wait_done(jid)
        time.sleep(0.01)
    removed = jobs.prune(keep=2)
    assert removed == 2
    assert len(jobs.list_jobs()) == 2


# --------------------------------------------------------------------------- #
# reverse-proxy sites (no nginx, no aaPanel)
# --------------------------------------------------------------------------- #
def test_set_site_writes_vhost_via_nginx_fallback(tmp_path, monkeypatch):
    vdir = str(tmp_path / "vhost")
    monkeypatch.setattr(proxy, "VHOST_DIR", vdir)
    # aaPanel path unavailable -> fallback to nginx vhost
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": False, "path": "aapanel", "detail": "no panel"})
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    monkeypatch.setattr(proxy, "reload_nginx", lambda: True)
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)

    res = proxy.set_site("demo", "demo.5d.bisotech.in", 8080)
    assert res["domain"] == "demo.5d.bisotech.in"
    assert res["url"] == "http://demo.5d.bisotech.in/"
    assert res["via"] == "nginx-vhost"

    conf = os.path.join(vdir, "demo.conf")
    assert os.path.isfile(conf)
    body = open(conf, encoding="utf-8").read()
    assert "server_name demo.5d.bisotech.in;" in body
    assert "proxy_pass http://127.0.0.1:8080;" in body


def test_set_site_prefers_aapanel_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": True, "path": "aapanel", "detail": "via panelSite.add"})
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)
    res = proxy.set_site("demo", "demo.5d.bisotech.in", 8081)
    assert res["via"] == "aapanel"
    # nginx vhost must NOT be written when aaPanel succeeds
    assert not os.path.isfile(os.path.join(str(tmp_path / "vhost"), "demo.conf"))


def test_default_domain_convention():
    assert proxy.default_domain("myapp") == "myapp.5d.bisotech.in"


def test_store_and_read_domain_marker(tmp_path, monkeypatch):
    from core.tomcat import instance
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "demo", "bin"), exist_ok=True)
    assert proxy.read_domain("demo") is None
    proxy._store_domain("demo", "demo.5d.bisotech.in")
    assert proxy.read_domain("demo") == "demo.5d.bisotech.in"
    proxy._clear_domain("demo")
    assert proxy.read_domain("demo") is None
