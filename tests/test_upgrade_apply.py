# coding: utf-8
"""The automatic part of an upgrade: only what can be proven unchanged."""
import json
import os

import pytest

from test_upgrade_report import LEGACY, PANEL_SITE, box, _by_app  # noqa: F401

from core import jobs, upgrade
from core.deploy import proxy
from core.deploy import ssl as sslmod


@pytest.fixture
def rig(box, monkeypatch):
    """nginx stand-in: answers depend on which files exist right now."""
    state = {"reloads": 0, "nginx_ok": True, "answer": None, "jobs": []}
    monkeypatch.setattr(upgrade, "STATE_PATH", str(box.tmp / "upgrade.json"))
    monkeypatch.setattr(upgrade, "JOURNAL_PATH", str(box.tmp / "upgrade-move.json"))
    monkeypatch.setattr(upgrade, "BACKUP_ROOT", str(box.tmp / "upgrade-backup"))
    monkeypatch.setattr(upgrade, "_SETTLE", (0, 0))
    monkeypatch.setattr(upgrade, "_BEFORE", (0, 0))
    monkeypatch.setattr(upgrade, "_started", False)
    monkeypatch.setattr(proxy, "nginx_test", lambda: state["nginx_ok"])
    monkeypatch.setattr(jobs, "start", lambda kind, target, argv: state["jobs"].append(kind) or "job-1")

    def reload_nginx():
        state["reloads"] += 1
        return state["nginx_ok"]
    monkeypatch.setattr(proxy, "reload_nginx", reload_nginx)

    def probe(host, https, body=False):
        if state["answer"]:
            return state["answer"](host, https)
        if https:
            return None
        return ((404, None, "application/json", None, None),) * 2
    monkeypatch.setattr(upgrade, "_probe", probe)
    box.state = state
    return box


def _legacy(rig, app, port, owner=None, listen="listen 80;"):
    rig.app(app, port)
    dom = "%s.example.com" % app
    (rig.legacy / ("%s.conf" % app)).write_text(
        (LEGACY % dict(app=app, domain=dom, port=port)).replace("listen 80;", listen))
    proxy._store_domain(app, dom)
    if owner:
        proxy._store_owner(app, owner)
    return dom


def test_old_site_file_moves_and_nginx_conf_is_not_edited(rig):
    _legacy(rig, "shop", 8080)
    original = (rig.legacy / "shop.conf").read_bytes()
    conf = rig.conf.read_bytes()
    res = upgrade.apply()
    assert res["moved"] == ["shop"] and res["marked"] == ["shop"] and res["skipped"] == {}
    assert not (rig.legacy / "shop.conf").exists()
    assert (rig.vdir / "javahost_shop.conf").read_bytes() == original     # byte for byte
    assert proxy.read_owner("shop") == "javahost"
    assert rig.conf.read_bytes() == conf
    assert os.listdir(res["backup"]) == ["shop.conf"]
    assert not os.path.exists(upgrade.JOURNAL_PATH)
    assert upgrade.needed() is False and upgrade.apply(once=True) is None
    rep = upgrade.report()
    assert _by_app(rep)["shop"]["state"] == "site-file" and rep["counts"]["to_move"] == 0


def test_the_production_layout_is_left_exactly_alone(rig):
    rig.app("Acquiring", 8000)
    rig.app("Portal", 8001, edit=("NoNewPrivileges=true", "NoNewPrivileges=false"))
    (rig.legacy / "Portal.conf").write_text(LEGACY % dict(app="Portal", domain="portal.example.com", port=8001))
    (rig.vdir / "portal.example.com.conf").write_text(PANEL_SITE % dict(domain="portal.example.com", extra=""))
    proxy._store_domain("Portal", "portal.example.com")
    sslmod._mark_ssl("Portal", True)
    before = dict(rig.snapshot())
    upgrade.start_if_needed()                     # what a status request does
    assert rig.state["jobs"] == []                # nothing to do, so no job at all
    res = upgrade.apply()
    assert res["moved"] == [] and res["marked"] == [] and res["backup"] is None
    after = dict(rig.snapshot())
    after.pop(upgrade.STATE_PATH, None)
    assert after == before                        # nothing else exists or changed
    assert rig.state["reloads"] == 0              # nginx was not even reloaded


def test_a_different_answer_after_the_move_puts_everything_back(rig):
    _legacy(rig, "shop", 8080, owner="javahost")
    original = (rig.legacy / "shop.conf").read_bytes()

    def answer(host, https):
        if https:
            return None
        moved = (rig.vdir / "javahost_shop.conf").exists()
        return ((301 if moved else 404, None, "text/html", None, None),) * 2
    rig.state["answer"] = answer
    res = upgrade.apply()
    assert res["moved"] == [] and "answered differently" in res["skipped"]["shop"]
    assert "RESTORE NOT CONFIRMED" not in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").read_bytes() == original
    assert not (rig.vdir / "javahost_shop.conf").exists()
    assert not os.path.exists(upgrade.JOURNAL_PATH)


def test_a_change_of_the_servers_default_site_is_caught(rig):
    _legacy(rig, "shop", 8080, owner="javahost")

    def answer(host, https):
        if https:
            return None
        moved = (rig.vdir / "javahost_shop.conf").exists()
        if host == upgrade._UNKNOWN_HOST:       # an unknown name lands elsewhere after the move
            return ((404 if moved else 200, None, "text/html", "x", None),) * 2
        return ((404, None, "application/json", None, None),) * 2
    rig.state["answer"] = answer
    res = upgrade.apply()
    assert "default site, answered differently" in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").exists() and not (rig.vdir / "javahost_shop.conf").exists()


@pytest.mark.parametrize("listen", ["listen 10.0.0.5:80;", "listen 8080;",
                                    "listen 80 default_server;", "listen 80 reuseport;"])
def test_only_plain_listens_are_moved(rig, listen):
    _legacy(rig, "shop", 8080, owner="javahost", listen=listen)
    res = upgrade.apply()
    assert res["moved"] == [] and "cannot be checked" in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").exists() and rig.state["reloads"] == 0


def test_plain_listen_forms():
    ok = "listen 80;\nlisten [::]:80;\nlisten 443 ssl http2;\nlisten [::]:443 ssl;\n# listen 81;\n"
    assert upgrade._plain_listens(ok)
    assert not upgrade._plain_listens("server_name x;\n")


@pytest.mark.parametrize("setup,reason", [
    (lambda r: r.state.update(nginx_ok=False), "config check fails"),
    (lambda r: r.state.update(answer=lambda d, h: None), "nothing could be proven"),
    (lambda r: r.conf.write_text("http {\n}\n"), ""),          # report itself says keep
])
def test_no_proof_means_no_move(rig, setup, reason):
    _legacy(rig, "shop", 8080, owner="javahost")
    setup(rig)
    res = upgrade.apply()
    assert res["moved"] == [] and reason in res["skipped"].get("shop", "")
    assert (rig.legacy / "shop.conf").exists() and not (rig.vdir / "javahost_shop.conf").exists()


def test_answers_that_flap_are_not_compared(rig):
    _legacy(rig, "shop", 8080, owner="javahost")
    n = {"i": 0}

    def answer(host, https):
        if https:
            return None
        n["i"] += 1
        return ((200 + n["i"], None, "text/html", None, None),) * 2
    rig.state["answer"] = answer
    res = upgrade.apply()
    assert "nothing could be proven" in res["skipped"]["shop"] and rig.state["reloads"] == 0


def test_reload_failure_after_the_move_is_undone_and_flagged_if_undo_fails(rig, monkeypatch):
    _legacy(rig, "shop", 8080, owner="javahost")
    calls = {"n": 0}

    def reload_nginx():
        calls["n"] += 1
        return calls["n"] > 1            # the reload after the move fails; the undo's works
    monkeypatch.setattr(proxy, "reload_nginx", reload_nginx)
    res = upgrade.apply()
    assert "did not accept" in res["skipped"]["shop"] and "RESTORE" not in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").exists() and not (rig.vdir / "javahost_shop.conf").exists()

    monkeypatch.setattr(upgrade, "STATE_PATH", str(rig.tmp / "upgrade2.json"))
    monkeypatch.setattr(proxy, "reload_nginx", lambda: False)      # nginx never comes back
    res = upgrade.apply()
    assert "RESTORE NOT CONFIRMED" in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").exists()                     # the file is back regardless


def test_a_killed_worker_leaves_a_journal(rig, monkeypatch):
    _legacy(rig, "shop", 8080, owner="javahost")

    def killed():
        raise SystemExit(1)              # not an Exception: a killed worker
    monkeypatch.setattr(proxy, "reload_nginx", killed)
    monkeypatch.setattr(upgrade, "_restore", lambda *a: False)     # and the undo never finished
    with pytest.raises(SystemExit):
        upgrade.apply()
    assert os.path.exists(upgrade.JOURNAL_PATH)                    # the next run will undo it


def test_recover_restores_from_the_copy(rig):
    _legacy(rig, "shop", 8080, owner="javahost")
    old, new = proxy.legacy_vhost_path("shop"), proxy.vhost_path("shop")
    original = open(old, "rb").read()
    copy = str(rig.tmp / "copy.conf"); open(copy, "wb").write(original)
    os.rename(old, new)                                            # the half-done state
    upgrade._journal({"app": "shop", "old": old, "new": new, "copy": copy})
    assert upgrade._recover() is None
    assert open(old, "rb").read() == original and not os.path.exists(new)
    assert not os.path.exists(upgrade.JOURNAL_PATH)


def test_status_request_only_starts_a_job_and_only_once(rig):
    _legacy(rig, "shop", 8080)
    before = (rig.legacy / "shop.conf").read_bytes()
    upgrade.start_if_needed(); upgrade.start_if_needed()
    assert rig.state["jobs"] == ["upgrade"]                        # one job, nothing moved inline
    assert (rig.legacy / "shop.conf").read_bytes() == before and rig.state["reloads"] == 0
    assert upgrade.last_run()["pending"] is True and upgrade.needed() is False
    res = upgrade.apply(once=True)                                 # what the job runs
    assert res["moved"] == ["shop"] and "pending" not in upgrade.last_run()


def test_skipped_work_is_retried_a_few_times_then_left(rig, monkeypatch):
    _legacy(rig, "shop", 8080, owner="javahost")
    rig.state["nginx_ok"] = False
    assert upgrade.apply()["attempts"] == 1
    assert upgrade.needed() is False                               # not straight away
    st = upgrade._state(); st["ts"] -= upgrade._RETRY_AFTER + 1; upgrade._save_state(st)
    assert upgrade.needed() is True
    assert upgrade.apply(once=True)["attempts"] == 2
    st = upgrade._state(); st["ts"] -= upgrade._RETRY_AFTER + 1; st["attempts"] = 3; upgrade._save_state(st)
    assert upgrade.needed() is False                               # gave up; RunUpgrade still works


def test_plan_changes_nothing(rig):
    _legacy(rig, "shop", 8080)
    before = dict(rig.snapshot())
    res = upgrade.apply(dry_run=True)
    assert res["dry_run"] and res["moved"] == ["shop"]
    assert dict(rig.snapshot()) == before


def test_start_if_needed_never_raises(rig, monkeypatch):
    monkeypatch.setattr(upgrade, "report", lambda: 1 / 0)
    upgrade.start_if_needed()
