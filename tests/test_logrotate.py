# coding: utf-8
"""Offline tests for log rotation + purge. No cron, no panel — INSTANCE_ROOT and
LOGS_DIR are redirected into tmp; CRON_PATH is pointed at a tmp file so the
schedule writer never touches the real /etc/cron.d."""
import gzip
import os
import time

from core import logrotate


def _setup(tmp_path, monkeypatch):
    inst = tmp_path / "instances"
    logs = tmp_path / "logs"
    cron = tmp_path / "cron.d"
    cron.mkdir()
    monkeypatch.setattr(logrotate, "INSTANCE_ROOT", str(inst))
    monkeypatch.setattr(logrotate, "LOGS_DIR", str(logs))
    monkeypatch.setattr(logrotate, "CRON_PATH", str(cron / "javahost-logrotate"))
    monkeypatch.setattr(logrotate, "CRON_LOG", str(logs / "logrotate-cron.log"))
    return inst, logs, cron


def _make_log(inst, app, name, size):
    ldir = inst / app / "logs"
    ldir.mkdir(parents=True, exist_ok=True)
    p = ldir / name
    p.write_bytes(b"L" * size)
    return str(p)


def test_rotate_copytruncate_gzips_and_empties_live(tmp_path, monkeypatch):
    inst, _, _ = _setup(tmp_path, monkeypatch)
    big = _make_log(inst, "app1", "catalina.out", 2 * 1024 * 1024)
    small = _make_log(inst, "app1", "app.log", 10)
    res = logrotate.rotate(max_mb=1, keep=3)
    assert res["count"] == 1 and big in res["rotated"]
    assert os.path.getsize(big) == 0                 # live file truncated in place
    assert os.path.exists(big + ".1.gz")             # rotated copy created
    assert os.path.getsize(small) == 10              # under limit, untouched
    # gz really holds the old content
    with gzip.open(big + ".1.gz", "rb") as f:
        assert f.read() == b"L" * (2 * 1024 * 1024)


def test_rotate_keeps_only_n(tmp_path, monkeypatch):
    inst, _, _ = _setup(tmp_path, monkeypatch)
    p = _make_log(inst, "app1", "catalina.out", 2 * 1024 * 1024)
    for _ in range(5):
        p_again = _make_log(inst, "app1", "catalina.out", 2 * 1024 * 1024)
        logrotate.rotate(max_mb=1, keep=3)
    gzs = sorted(f for f in os.listdir(os.path.dirname(p)) if f.endswith(".gz"))
    assert gzs == ["catalina.out.1.gz", "catalina.out.2.gz", "catalina.out.3.gz"]


def test_purge_removes_old_rotations_only(tmp_path, monkeypatch):
    inst, _, _ = _setup(tmp_path, monkeypatch)
    live = _make_log(inst, "app1", "catalina.out", 50)
    old = live + ".2.gz"
    new = live + ".1.gz"
    open(old, "wb").write(b"x"); open(new, "wb").write(b"y")
    # age the old rotation 40 days back
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    res = logrotate.purge(days=30)
    assert res["removed"] == 1
    assert not os.path.exists(old)      # old gz gone
    assert os.path.exists(new)          # recent gz kept
    assert os.path.exists(live)         # live log never touched


def test_purge_all_when_zero_days(tmp_path, monkeypatch):
    inst, _, _ = _setup(tmp_path, monkeypatch)
    live = _make_log(inst, "app1", "catalina.out", 50)
    open(live + ".1.gz", "wb").write(b"y")
    res = logrotate.purge(days=0)
    assert res["removed"] == 1
    assert not os.path.exists(live + ".1.gz")
    assert os.path.exists(live)


def test_apply_schedule_writes_and_removes_cron(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from core import config
    monkeypatch.setattr(config, "log_rotate_enabled", lambda: True)
    monkeypatch.setattr(config, "log_rotate_when", lambda: "weekly")
    monkeypatch.setattr(config, "get", lambda k, d=None: False if k == "manage_hardening" else d)
    res = logrotate.apply_schedule()
    assert res["cron"] is True
    body = open(logrotate.CRON_PATH).read()
    assert "0 0 * * 0 root" in body and "--run" in body
    # disabling removes the file
    monkeypatch.setattr(config, "log_rotate_enabled", lambda: False)
    res2 = logrotate.apply_schedule()
    assert res2["enabled"] is False
    assert not os.path.exists(logrotate.CRON_PATH)


def test_status_reports_sizes(tmp_path, monkeypatch):
    inst, _, _ = _setup(tmp_path, monkeypatch)
    _make_log(inst, "app1", "catalina.out", 1234)
    open(str(inst / "app1" / "logs" / "catalina.out.1.gz"), "wb").write(b"z" * 100)
    st = logrotate.status()
    assert st["live_bytes"] == 1234
    assert st["rotated_bytes"] == 100 and st["rotated_files"] == 1
