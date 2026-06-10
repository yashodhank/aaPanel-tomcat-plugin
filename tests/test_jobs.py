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
    assert set(rec.keys()) == {"id", "state", "message", "log", "exists"}
    assert rec["exists"] is True
    meta = jobs.list_jobs()[0]
    for k in ("id", "kind", "target", "state", "started", "ended", "message"):
        assert k in meta


def test_read_log_missing_job_reports_not_running(tmp_path, monkeypatch):
    """A pruned/never-created job must report exists=False and a non-running
    state so the UI shows 'no longer available' and STOPS polling (the old
    behaviour returned state='' which the frontend treated as running)."""
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    rec = jobs.read_log("never-existed-20200101T000000Z-abc123")
    assert rec["exists"] is False
    assert rec["state"] == "missing"
    assert rec["log"] == ""


def test_count_skipped_counts_corrupt_dirs(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    good = jobs.start("test", "g", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(good)
    # a dir with a valid-looking id but no/garbage meta.json
    os.makedirs(str(root / "broken-20200101T000000Z-deadbe"), exist_ok=True)
    assert jobs.count_skipped() == 1
    assert good in [m["id"] for m in jobs.list_jobs()]


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


def _wait_running_with_pid(job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            meta = jobs.list_jobs()[0]
        except IndexError:
            meta = {}
        if meta.get("state") == "running" and meta.get("pid"):
            return meta
        time.sleep(0.05)
    return jobs.list_jobs()[0] if jobs.list_jobs() else {}


def test_cancel_running_job(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    sleeper = [jobs.sys.executable or "python3", "-c", "import time; time.sleep(60)"]
    job_id = jobs.start("test", "sleep", sleeper)
    meta = _wait_running_with_pid(job_id)
    assert meta.get("state") == "running" and meta.get("pid")
    res = jobs.cancel(job_id)
    assert res["state"] == "cancelled"
    rec = jobs.read_log(job_id)
    assert rec["state"] == "cancelled"
    assert "cancelled" in rec["message"]
    # the supervisor's process group is gone
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(os.getpgid(int(meta["pid"])), 0)


def test_cancel_escalates_to_sigkill(tmp_path, monkeypatch):
    """Work that IGNORES SIGTERM must still be killed (SIGKILL escalation) — the
    recorded 'cancelled' state must mean the process is actually gone."""
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    job_id = jobs.start("test", "stubborn", [jobs.sys.executable or "python3", "-c", code])
    meta = _wait_running_with_pid(job_id)
    pid = int(meta["pid"])
    res = jobs.cancel(job_id)            # SIGTERM ignored -> escalates to SIGKILL
    assert res["state"] == "cancelled"
    time.sleep(0.3)
    with pytest.raises((ProcessLookupError, OSError)):
        os.killpg(os.getpgid(pid), 0)    # process group is gone


def test_cancel_keeps_terminal_state_on_natural_finish(tmp_path, monkeypatch):
    """If the supervisor finalizes the job (done/failed) in the window between the
    initial state check and the kill, cancel() must respect that terminal state,
    not clobber a succeeded job to 'cancelled'."""
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    job_id = jobs.start("test", "q", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(job_id)
    base = jobs._read_meta(jobs.job_dir(job_id))
    seq = {"n": 0}

    def fake_read(jdir):
        seq["n"] += 1
        m = dict(base)
        m["state"] = "running" if seq["n"] == 1 else "done"   # finishes during cancel
        m["pid"] = 999999
        return m

    monkeypatch.setattr(jobs, "_read_meta", fake_read)
    monkeypatch.setattr(jobs.os, "getpgid",
                        lambda p: (_ for _ in ()).throw(ProcessLookupError()))
    res = jobs.cancel(job_id)
    assert res["state"] == "done"     # natural finish respected, not overwritten


def test_cancel_finished_job_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    job_id = jobs.start("test", "x", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(job_id)
    with pytest.raises(ValueError):
        jobs.cancel(job_id)


def test_retry_reruns_recorded_command(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    job_id = jobs.start("test", "r", [jobs.sys.executable or "python3", "-c", "print('again-please')"])
    _wait_done(job_id)
    new_id = jobs.retry(job_id)
    assert new_id != job_id
    rec = _wait_done(new_id)
    assert rec["state"] == "done"
    assert "again-please" in rec["log"]
    new_meta = [m for m in jobs.list_jobs() if m["id"] == new_id][0]
    assert new_meta["kind"] == "test" and new_meta["target"] == "r"


def test_clear_removes_finished_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    done_id = jobs.start("test", "d", [jobs.sys.executable or "python3", "-c", "print(1)"])
    _wait_done(done_id)
    run_id = jobs.start("test", "s", [jobs.sys.executable or "python3", "-c", "import time; time.sleep(60)"])
    _wait_running_with_pid(run_id)
    removed = jobs.clear()
    assert removed == 1
    ids = [m["id"] for m in jobs.list_jobs()]
    assert run_id in ids and done_id not in ids
    jobs.cancel(run_id)  # don't leak the sleeper


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
def test_set_site_errors_when_aapanel_unavailable(tmp_path, monkeypatch):
    vdir = str(tmp_path / "vhost")
    monkeypatch.setattr(proxy, "VHOST_DIR", vdir)
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": False, "path": "aapanel",
                                      "detail": "no panel"})
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)

    res = proxy.set_site("demo", "demo.5d.bisotech.in", 8080)
    assert res["ok"] is False
    assert "aaPanel site registration failed" in res["error"]
    assert not os.path.isfile(os.path.join(vdir, "demo.conf"))


def test_set_site_prefers_aapanel_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": True, "path": "aapanel",
                                      "detail": "via site.AddSite"})
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    res = proxy.set_site("demo", "demo.5d.bisotech.in", 8081)
    assert res["ok"] is True
    assert res["via"] == "aapanel"
    assert not os.path.isfile(os.path.join(str(tmp_path / "vhost"), "demo.conf"))


def test_default_domain_convention(monkeypatch):
    # No baked-in suffix any more: default_domain is None unless a site_suffix is
    # configured, then it is "<app>.<suffix>" (de-hardcode of the old FQDN).
    from core import config
    monkeypatch.setattr(config, "site_suffix", lambda: "")
    assert proxy.default_domain("myapp") is None
    monkeypatch.setattr(config, "site_suffix", lambda: "example.com")
    assert proxy.default_domain("myapp") == "myapp.example.com"


def test_store_and_read_domain_marker(tmp_path, monkeypatch):
    from core.tomcat import instance
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "demo", "bin"), exist_ok=True)
    assert proxy.read_domain("demo") is None
    proxy._store_domain("demo", "demo.5d.bisotech.in")
    assert proxy.read_domain("demo") == "demo.5d.bisotech.in"
    proxy._clear_domain("demo")
    assert proxy.read_domain("demo") is None
