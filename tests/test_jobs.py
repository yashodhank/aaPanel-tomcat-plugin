# coding: utf-8
"""Offline tests for the background-job runner and reverse-proxy site endpoints.
No network, no panel, no nginx — JOBS_ROOT and VHOST_DIR are redirected to tmp."""
import json
import os
import threading
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


def test_list_jobs_skips_symlinked_job_directories(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    outside = tmp_path / "outside-job"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    os.makedirs(outside)
    (outside / "meta.json").write_text(
        json.dumps({"id": "outside", "state": "done", "started": 99}),
        encoding="utf-8",
    )
    os.makedirs(root)
    os.symlink(outside, root / "linked-job")

    assert jobs.list_jobs() == []
    assert jobs.count_skipped() == 0


def _write_fake_proc_stat(
        proc_root, pid, state="S", pgrp=0, starttime="987", comm="python worker"):
    pid_root = proc_root / str(pid)
    os.makedirs(pid_root)
    # stat fields after comm begin at field 3: state, ppid, pgrp, ... starttime.
    # A right parenthesis in comm proves parsers split after the *final* ')'.
    stat_tail = [state, "0", str(pgrp)] + (["0"] * 16) + [starttime]
    (pid_root / "stat").write_text(
        "%d (%s) %s\n" % (pid, comm, " ".join(stat_tail)),
        encoding="ascii",
    )
    return pid_root


def _write_fake_proc_identity(proc_root, pid, argv, starttime="987"):
    pid_root = _write_fake_proc_stat(proc_root, pid, starttime=starttime)
    (pid_root / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")


def test_capture_supervisor_identity_requires_exact_proc_argv(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    jdir = os.path.realpath(str(tmp_path / "job"))
    os.makedirs(jdir)
    pid = 123
    expected_argv = [
        jobs.sys.executable or "python3",
        os.path.realpath(jobs.__file__),
        "exec",
        jdir,
        "--",
        "python3",
        "-c",
        "print(1)",
    ]
    _write_fake_proc_identity(proc_root, pid, expected_argv)
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))

    assert jobs._capture_supervisor_identity(pid, jdir) == (pid, 987)


@pytest.mark.parametrize("argv", [
    ["python3", "-c", "text mentioning jobs.py exec /tmp/job --"],
    ["python3", "jobs.py", "exec", "/wrong/job", "--"],
    ["python3", "jobs.py", "exec", "/tmp/job"],
])
def test_capture_supervisor_identity_rejects_wrong_or_malformed_argv(
        tmp_path, monkeypatch, argv):
    proc_root = tmp_path / "proc"
    jdir = os.path.realpath(str(tmp_path / "job"))
    os.makedirs(jdir)
    pid = 456
    _write_fake_proc_identity(proc_root, pid, argv)
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))

    assert jobs._capture_supervisor_identity(pid, jdir) is None


def test_capture_supervisor_identity_fails_closed_when_proc_hidden(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    jdir = os.path.realpath(str(tmp_path / "job"))
    os.makedirs(jdir)
    pid = 789
    _write_fake_proc_identity(proc_root, pid, ["unused"])
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))
    real_open = open

    def hidden_open(path, *args, **kwargs):
        if str(path).endswith("/cmdline"):
            raise PermissionError("proc hidden")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", hidden_open)

    assert jobs._capture_supervisor_identity(pid, jdir) is None


def test_process_group_exists_treats_all_zombie_members_as_absent(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    os.makedirs(proc_root)
    _write_fake_proc_stat(proc_root, 101, state="Z", pgrp=4321)
    _write_fake_proc_stat(
        proc_root, 102, state="Z", pgrp=4321, comm="worker ) with paren"
    )
    _write_fake_proc_stat(proc_root, 103, state="S", pgrp=9999)
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)

    assert jobs._process_group_exists(4321) is False


def test_process_group_exists_detects_live_member_among_zombies(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    os.makedirs(proc_root)
    _write_fake_proc_stat(proc_root, 201, state="Z", pgrp=4321)
    _write_fake_proc_stat(proc_root, 202, state="S", pgrp=4321)
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)

    assert jobs._process_group_exists(4321) is True


def test_process_group_exists_fails_closed_on_malformed_proc_stat(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    os.makedirs(proc_root / "301")
    (proc_root / "301" / "stat").write_bytes(b"301 malformed\n")
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)

    with pytest.raises(RuntimeError, match="malformed proc stat"):
        jobs._process_group_exists(4321)


def test_process_group_exists_fails_closed_when_proc_stat_unreadable(
        tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    os.makedirs(proc_root)
    _write_fake_proc_stat(proc_root, 401, state="Z", pgrp=4321)
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)
    real_open = open

    def denied_open(path, *args, **kwargs):
        if str(path).endswith("/401/stat"):
            raise PermissionError("proc hidden")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied_open)

    with pytest.raises(RuntimeError, match="permission denied"):
        jobs._process_group_exists(4321)


def test_process_group_exists_fails_closed_when_visible_members_do_not_match(
        tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    os.makedirs(proc_root)
    _write_fake_proc_stat(proc_root, 501, state="S", pgrp=9999)
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc_root))
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)

    with pytest.raises(RuntimeError, match="no members were visible"):
        jobs._process_group_exists(4321)


def test_write_meta_uses_unique_temp_files_under_concurrency(tmp_path):
    jdir = str(tmp_path / "job")
    barrier = threading.Barrier(8)
    errors = []

    def writer(index):
        try:
            barrier.wait()
            jobs._write_meta(jdir, {"writer": index})
        except Exception as exc:  # captured so thread failures fail the test
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert jobs._read_meta(jdir)["writer"] in range(8)
    assert os.listdir(jdir) == ["meta.json"]


def test_malformed_state_and_started_are_normalized_and_preserved(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    jobs._write_meta(jobs.job_dir("done-job"), {
        "id": "forged-id",
        "state": "DONE",
        "started": 10,
    })
    jobs._write_meta(jobs.job_dir("malformed-job"), {
        "id": "malformed-job",
        "state": {"unexpected": "mapping"},
        "started": {"unexpected": "mapping"},
    })

    metas = jobs.list_jobs(limit=100)

    assert [meta["id"] for meta in metas] == ["done-job", "malformed-job"]
    assert metas[0]["state"] == "done"
    assert metas[1]["state"] == "unknown"
    assert metas[1]["started"] == 0.0
    assert jobs.prune(keep=0) == 1
    assert jobs.clear() == 0
    assert [meta["id"] for meta in jobs.list_jobs()] == ["malformed-job"]


def test_non_mapping_metadata_does_not_break_list_prune_or_clear(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    jdir = root / "broken-job"
    os.makedirs(jdir)
    (jdir / "meta.json").write_text("[]", encoding="utf-8")

    assert jobs.list_jobs() == []
    assert jobs.count_skipped() == 1
    assert jobs.prune(keep=0) == 0
    assert jobs.clear() == 0


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
    # macOS sandboxing can deny both procfs and ps inspection; identity probing
    # has its own unit coverage, while this test exercises signal + state flow.
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: True)
    monkeypatch.setattr(jobs, "_wait_for_process_group_exit", lambda pgid, timeout: True)
    sleeper = [jobs.sys.executable or "python3", "-c", "import time; time.sleep(60)"]
    job_id = jobs.start("test", "sleep", sleeper)
    meta = _wait_running_with_pid(job_id)
    assert meta.get("state") == "running" and meta.get("pid")
    res = jobs.cancel(job_id)
    assert res["state"] == "cancelled"
    rec = jobs.read_log(job_id)
    assert rec["state"] == "cancelled"
    assert "cancelled" in rec["message"]
    # No executable member remains. Linux may retain an unreaped zombie briefly,
    # so killpg(..., 0) alone is not a correct exit assertion.
    time.sleep(0.2)
    try:
        pgid = os.getpgid(int(meta["pid"]))
    except ProcessLookupError:
        pass
    else:
        assert jobs._process_group_exists(pgid) is False


@pytest.mark.skipif(
    not jobs.sys.platform.startswith("linux") or not os.path.isdir("/proc"),
    reason="real supervisor identity verification requires Linux procfs",
)
def test_cancel_running_job_with_real_linux_proc_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    sleeper = [jobs.sys.executable or "python3", "-c", "import time; time.sleep(60)"]
    job_id = jobs.start("test", "linux-proc", sleeper)
    meta = _wait_running_with_pid(job_id)
    try:
        identity = jobs._capture_supervisor_identity(meta.get("pid"), jobs.job_dir(job_id))
        if identity is None:
            pytest.skip("procfs is mounted but supervisor records are hidden")
        result = jobs.cancel(job_id)
    finally:
        # Exact fresh PID created by this test; prevent a sleeper leak if the
        # environment exposes procfs but denies cancellation signals/probes.
        try:
            os.killpg(os.getpgid(int(meta["pid"])), jobs.signal.SIGKILL)
        except (KeyError, OSError, TypeError, ValueError):
            pass

    assert result["state"] == "cancelled"
    assert jobs.read_log(job_id)["state"] == "cancelled"


@pytest.mark.skipif(
    not jobs.sys.platform.startswith("linux") or not os.path.isdir("/proc"),
    reason="real stubborn-job escalation requires Linux procfs",
)
def test_cancel_stubborn_job_with_real_linux_proc_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('worker-ready', flush=True); time.sleep(60)"
    )
    job_id = jobs.start(
        "test", "linux-stubborn", [jobs.sys.executable or "python3", "-c", code]
    )
    meta = _wait_running_with_pid(job_id)
    signals = []
    real_signal_group = jobs._signal_process_group

    def recording_signal_group(pgid, sig):
        signals.append(sig)
        return real_signal_group(pgid, sig)

    monkeypatch.setattr(jobs, "_signal_process_group", recording_signal_group)
    try:
        identity = jobs._capture_supervisor_identity(
            meta.get("pid"), jobs.job_dir(job_id)
        )
        if identity is None:
            pytest.skip("procfs is mounted but supervisor records are hidden")
        deadline = time.time() + 5
        while "worker-ready" not in jobs.read_log(job_id)["log"]:
            if time.time() >= deadline:
                pytest.fail("stubborn worker did not become ready")
            time.sleep(0.05)
        result = jobs.cancel(job_id)
    finally:
        # Always reap the exact fresh process group from this test if an assert,
        # procfs read, or cancellation step fails midway.
        try:
            os.killpg(os.getpgid(int(meta["pid"])), jobs.signal.SIGKILL)
        except (KeyError, OSError, TypeError, ValueError):
            pass

    assert result["state"] == "cancelled"
    assert jobs.read_log(job_id)["state"] == "cancelled"
    assert signals == [jobs.signal.SIGTERM, jobs.signal.SIGKILL]


def test_supervisor_survives_term_and_defers_final_state_to_canceller(tmp_path, monkeypatch):
    jdir = str(tmp_path / "job")
    jobs._write_meta(jdir, {
        "id": "term-job",
        "state": "running",
        "started": 1,
        "pid": None,
    })
    handlers = {}

    def install_handler(sig, handler):
        handlers[sig] = handler
        return jobs.signal.SIG_DFL

    class TerminatedWorker:
        def wait(self):
            handlers[jobs.signal.SIGTERM](jobs.signal.SIGTERM, None)
            return -jobs.signal.SIGTERM

    monkeypatch.setattr(jobs.signal, "signal", install_handler)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: TerminatedWorker())
    monkeypatch.setattr(
        jobs.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("supervisor must expose PID only after Popen"),
    )

    rc = jobs._supervise(jdir, ["python3", "-c", "print(1)"])

    assert rc == 128 + jobs.signal.SIGTERM
    assert jobs._read_meta(jdir)["state"] == "running"


def test_cancel_escalates_to_sigkill(tmp_path, monkeypatch):
    """Work that IGNORES SIGTERM must still be killed (SIGKILL escalation) — the
    recorded 'cancelled' state must mean the process is actually gone."""
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    job_id = "stubborn-job"
    jobs._write_meta(jobs.job_dir(job_id), {
        "id": job_id,
        "kind": "test",
        "target": "stubborn",
        "state": "running",
        "started": 1,
        "pid": 1234,
    })
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4321)
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: True)
    signals = []
    monkeypatch.setattr(
        jobs, "_signal_process_group",
        lambda pgid, sig: signals.append(sig) is None,
    )
    exits = iter((False, True))
    monkeypatch.setattr(
        jobs, "_wait_for_process_group_exit",
        lambda pgid, timeout: next(exits),
    )

    res = jobs.cancel(job_id)            # SIGTERM ignored -> escalates to SIGKILL

    assert res["state"] == "cancelled"
    assert signals == [jobs.signal.SIGTERM, jobs.signal.SIGKILL]


def test_cancel_permission_uncertainty_keeps_job_running(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    job_id = "permission-denied"
    jobs._write_meta(jobs.job_dir(job_id), {
        "id": job_id,
        "kind": "test",
        "target": "active",
        "state": "running",
        "started": 1,
        "pid": 1234,
    })
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4321)
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: True)

    def deny_signal(pgid, sig):
        raise PermissionError("not permitted")

    monkeypatch.setattr(jobs.os, "killpg", deny_signal)

    with pytest.raises(RuntimeError, match="permission"):
        jobs.cancel(job_id)

    assert jobs.read_log(job_id)["state"] == "running"
    assert jobs.prune(keep=0) == 0
    assert jobs.clear() == 0


def test_cancel_identity_probe_uncertainty_keeps_job_running(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    job_id = "identity-unknown"
    jobs._write_meta(jobs.job_dir(job_id), {
        "id": job_id,
        "state": "running",
        "started": 1,
        "pid": 1234,
    })
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4321)
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: None)
    monkeypatch.setattr(
        jobs.os,
        "killpg",
        lambda pgid, sig: pytest.fail("an unverified process group must not be signalled"),
    )

    with pytest.raises(RuntimeError, match="could not be verified"):
        jobs.cancel(job_id)

    assert jobs.read_log(job_id)["state"] == "running"
    assert jobs.prune(keep=0) == 0
    assert jobs.clear() == 0


def test_cancel_rejects_pid_birth_change_before_sigterm(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    job_id = "reused-before-term"
    jobs._write_meta(jobs.job_dir(job_id), {
        "id": job_id,
        "state": "running",
        "started": 1,
        "pid": 1234,
    })
    identities = iter(((1234, 10), (1234, 11)))
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: next(identities))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4321)
    monkeypatch.setattr(
        jobs.os,
        "killpg",
        lambda pgid, sig: pytest.fail("a reused PID must not be signalled"),
    )

    with pytest.raises(RuntimeError, match="changed before SIGTERM"):
        jobs.cancel(job_id)

    assert jobs.read_log(job_id)["state"] == "running"


def test_cancel_rejects_pid_birth_change_before_sigkill(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    job_id = "reused-before-kill"
    jobs._write_meta(jobs.job_dir(job_id), {
        "id": job_id,
        "state": "running",
        "started": 1,
        "pid": 1234,
    })
    identities = iter(((1234, 10), (1234, 10), (1234, 11)))
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: next(identities))
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4321)
    signals = []
    monkeypatch.setattr(jobs, "_signal_process_group", lambda pgid, sig: signals.append(sig) is None)
    monkeypatch.setattr(jobs, "_wait_for_process_group_exit", lambda pgid, timeout: False)

    with pytest.raises(RuntimeError, match="changed before SIGKILL"):
        jobs.cancel(job_id)

    assert signals == [jobs.signal.SIGTERM]
    assert jobs.read_log(job_id)["state"] == "running"


def test_cancel_unproven_group_exit_keeps_job_running(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    job_id = "still-alive"
    jobs._write_meta(jobs.job_dir(job_id), {
        "id": job_id,
        "kind": "test",
        "target": "active",
        "state": "running",
        "started": 1,
        "pid": 1234,
    })
    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: 4321)
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: True)
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(jobs, "_wait_for_process_group_exit", lambda pgid, timeout: False)

    with pytest.raises(RuntimeError, match="still exists"):
        jobs.cancel(job_id)

    assert jobs.read_log(job_id)["state"] == "running"
    assert jobs.prune(keep=0) == 0
    assert jobs.clear() == 0


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
    monkeypatch.setattr(jobs, "_pid_is_supervisor", lambda pid, jdir: True)
    monkeypatch.setattr(jobs, "_wait_for_process_group_exit", lambda pgid, timeout: True)
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


def test_prune_counts_only_terminal_jobs_and_keeps_nonterminal(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    records = (
        ("running-job", "running", 1),
        ("cancelling-job", "cancelling", 2),
        ("done-old", "done", 3),
        ("failed-middle", "failed", 4),
        ("cancelled-new", "cancelled", 5),
    )
    for job_id, state, started in records:
        jdir = jobs.job_dir(job_id)
        jobs._write_meta(jdir, {
            "id": job_id,
            "state": state,
            "started": started,
        })

    removed = jobs.prune(keep=1)

    assert removed == 2
    remaining = {meta["id"] for meta in jobs.list_jobs(limit=100)}
    assert remaining == {"running-job", "cancelling-job", "cancelled-new"}


def test_clear_removes_only_terminal_jobs(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    for job_id, state in (
        ("running-job", "running"),
        ("cancelling-job", "cancelling"),
        ("unknown-job", "waiting-for-worker"),
        ("done-job", "done"),
        ("failed-job", "failed"),
        ("cancelled-job", "cancelled"),
    ):
        jobs._write_meta(jobs.job_dir(job_id), {
            "id": job_id,
            "state": state,
            "started": 1,
        })

    removed = jobs.clear()

    assert removed == 3
    remaining = {meta["id"] for meta in jobs.list_jobs(limit=100)}
    assert remaining == {"running-job", "cancelling-job", "unknown-job"}


def test_prune_does_not_follow_untrusted_meta_id(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    jobs._write_meta(jobs.job_dir("different-job"), {
        "id": "different-job",
        "state": "running",
        "started": 2,
    })
    jobs._write_meta(jobs.job_dir("actual-job"), {
        "id": "different-job",
        "state": "done",
        "started": 1,
    })

    assert jobs.prune(keep=0) == 1
    remaining = {meta["id"] for meta in jobs.list_jobs(limit=100)}
    assert remaining == {"different-job"}


def test_start_enforces_default_terminal_retention(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(root))
    monkeypatch.setattr(jobs, "DEFAULT_TERMINAL_RETENTION", 2)
    for index in range(3):
        jobs._write_meta(jobs.job_dir("done-%d" % index), {
            "id": "done-%d" % index,
            "state": "done",
            "started": index + 1,
        })
    monkeypatch.setattr(jobs, "_spawn_detached", lambda jdir, argv: None)

    new_id = jobs.start("test", "new", ["python3", "-c", "print(1)"])

    metas = jobs.list_jobs(limit=100)
    assert sum(meta["state"] == "done" for meta in metas) == 2
    assert any(meta["id"] == new_id and meta["state"] == "running" for meta in metas)


# --------------------------------------------------------------------------- #
# reverse-proxy sites (no nginx, no aaPanel)
# --------------------------------------------------------------------------- #
def test_set_site_errors_when_aapanel_unavailable(tmp_path, monkeypatch):
    from core.compat import aapanel as panel_api
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    vdir = str(tmp_path / "vhost")
    monkeypatch.setattr(proxy.panel_api, "ensure_ws_map", lambda: True)
    monkeypatch.setattr(proxy, "VHOST_DIR", vdir)
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": False, "path": "aapanel",
                                      "detail": "no panel",
                                      "tried": ["class-api", "legacy-panelsite"]})
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)

    res = proxy.set_site("demo", "demo.5d.bisotech.in", 8080)
    assert res["ok"] is False
    assert "aaPanel site registration failed" in res["error"]
    assert not os.path.isfile(os.path.join(vdir, "demo.conf"))


def test_set_site_prefers_aapanel_when_available(tmp_path, monkeypatch):
    from core.compat import aapanel as panel_api
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    monkeypatch.setattr(proxy.panel_api, "ensure_ws_map", lambda: True)
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": True, "path": "aapanel",
                                      "detail": "via panelSite.CreateProxy"})
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)
    monkeypatch.setattr(proxy, "_store_owner", lambda app, owner: None)
    monkeypatch.setattr(proxy, "read_domain", lambda app: None)
    monkeypatch.setattr(proxy, "remove_vhost", lambda app: None)
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    monkeypatch.setattr(proxy, "reload_nginx", lambda: True)
    res = proxy.set_site("demo", "demo.example.com", 8081)
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
