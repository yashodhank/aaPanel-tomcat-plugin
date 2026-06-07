# coding: utf-8
"""Offline tests for scheduled backups (core/backup/schedule.py). No real cron:
SCHEDULES_PATH and CRON_PATH are redirected to tmp. Covers cron validation,
JSON<->cron.d round-trip, idempotent add/remove, and retention via prune."""
import os

import pytest

from core.backup import schedule, store


@pytest.fixture
def sched(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "SCHEDULES_PATH", str(tmp_path / "schedules.json"))
    monkeypatch.setattr(schedule, "CRON_PATH", str(tmp_path / "cron.d" / "javahost-backups"))
    monkeypatch.setattr(schedule, "CRON_LOG", str(tmp_path / "logs" / "backup-cron.log"))
    os.makedirs(str(tmp_path / "cron.d"), exist_ok=True)   # so _regenerate writes it
    return tmp_path


def test_validate_cron():
    assert schedule.validate_cron("0 2 * * *") == "0 2 * * *"
    assert schedule.validate_cron(" 0   */6 * * 0 ") == "0 */6 * * 0"
    for bad in ("0 2 * *", "0 2 * * * extra", "0 2 * * x", "* * * * ; rm -rf", ""):
        with pytest.raises(ValueError):
            schedule.validate_cron(bad)


def test_set_list_remove_round_trip(sched):
    schedule.set_schedule("app1", "0 2 * * *", remotes=["wasabi", "minio"], keep=5)
    schedule.set_schedule("app2", "0 3 * * 0", remotes=[], keep=3)
    items = {s["app"]: s for s in schedule.list_schedules()}
    assert items["app1"]["cron"] == "0 2 * * *" and items["app1"]["remotes"] == ["wasabi", "minio"] and items["app1"]["keep"] == 5
    assert items["app2"]["remotes"] == []
    # cron.d rendered with both apps + the runner + destinations/retention flags
    body = open(schedule.CRON_PATH).read()
    assert "--app app1 --remotes wasabi,minio --keep 5" in body
    assert "--app app2 --keep 3" in body
    assert body.startswith("# Managed by JavaHost")
    assert " root " in body  # cron.d requires a user field
    # detach a profile (e.g. on force-delete) drops it from the schedule
    schedule.detach_remote("minio")
    assert {s["app"]: s for s in schedule.list_schedules()}["app1"]["remotes"] == ["wasabi"]
    # idempotent update
    schedule.set_schedule("app1", "0 4 * * *", remotes=[], keep=10)
    items = {s["app"]: s for s in schedule.list_schedules()}
    assert items["app1"]["cron"] == "0 4 * * *" and items["app1"]["keep"] == 10
    # remove one
    assert schedule.remove_schedule("app1")["removed"] is True
    assert [s["app"] for s in schedule.list_schedules()] == ["app2"]
    schedule.remove_schedule("app2")
    assert not os.path.exists(schedule.CRON_PATH)


def test_legacy_remote_bool_renders_all(sched):
    # an old entry stored {remote: true} renders as --remotes all
    import json as _j
    schedule._write({"old": {"cron": "0 1 * * *", "remote": True, "keep": 1}})
    schedule._regenerate(schedule._read())
    assert "--remotes all" in open(schedule.CRON_PATH).read()
    assert schedule.list_schedules()[0]["remotes"] == ["all"]


def test_remove_missing_is_noop(sched):
    assert schedule.remove_schedule("ghost")["removed"] is False


def test_prune_keeps_newest_local(tmp_path, monkeypatch):
    broot = str(tmp_path / "backups")
    os.makedirs(broot, exist_ok=True)
    monkeypatch.setattr(store, "BACKUPS_ROOT", broot)
    from core.util import fs
    monkeypatch.setattr(fs, "MANAGED_ROOTS", tuple(fs.MANAGED_ROOTS) + (broot,))
    # fabricate 4 archive files with sortable timestamps (no remote configured)
    names = ["backup-app-2026010%dT000000Z.tar.gz" % i for i in (1, 2, 3, 4)]
    for n in names:
        open(os.path.join(broot, n), "wb").write(b"x")
    out = store.prune_backups("app", keep=2)
    assert len(out["removed"]) == 2
    remaining = sorted(os.listdir(broot))
    assert remaining == names[2:]  # two newest kept
