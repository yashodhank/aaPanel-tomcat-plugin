# coding: utf-8
"""The automatic part of an upgrade: only what can be proven unchanged."""
import json
import os

import pytest

from test_upgrade_report import LEGACY, PANEL_SITE, box, _by_app  # noqa: F401

from core import upgrade
from core.deploy import proxy
from core.deploy import ssl as sslmod


@pytest.fixture
def rig(box, monkeypatch):
    """nginx stand-in: answers depend on which files exist right now."""
    state = {"reloads": 0, "nginx_ok": True, "answer": None}
    monkeypatch.setattr(upgrade, "STATE_PATH", str(box.tmp / "upgrade.json"))
    monkeypatch.setattr(upgrade, "BACKUP_ROOT", str(box.tmp / "upgrade-backup"))
    monkeypatch.setattr(proxy, "nginx_test", lambda: state["nginx_ok"])

    def reload_nginx():
        state["reloads"] += 1
        return state["nginx_ok"]
    monkeypatch.setattr(proxy, "reload_nginx", reload_nginx)

    def probe(domain, https):
        if state["answer"]:
            return state["answer"](domain, https)
        return ((404, None, "application/json"),) * 2 if not https else None
    monkeypatch.setattr(upgrade, "_probe", probe)
    box.state = state
    return box


def _legacy(rig, app, port, owner=None):
    rig.app(app, port)
    dom = "%s.example.com" % app
    (rig.legacy / ("%s.conf" % app)).write_text(LEGACY % dict(app=app, domain=dom, port=port))
    proxy._store_domain(app, dom)
    if owner:
        proxy._store_owner(app, owner)
    return dom


def test_old_site_file_moves_and_the_include_goes(rig):
    _legacy(rig, "shop", 8080)
    original = (rig.legacy / "shop.conf").read_text()
    res = upgrade.apply()
    assert res["moved"] == ["shop"] and res["marked"] == ["shop"] and res["skipped"] == {}
    assert not (rig.legacy / "shop.conf").exists()
    assert (rig.vdir / "javahost_shop.conf").read_text() == original      # byte for byte
    assert proxy.read_owner("shop") == "javahost"
    assert res["include_removed"] is True
    assert str(rig.legacy) not in rig.conf.read_text()
    # every touched file was copied first
    kept = os.listdir(res["backup"])
    assert any(k.endswith("shop.conf") for k in kept) and any(k.endswith("nginx.conf") for k in kept)
    # recorded, so it runs once per version
    assert upgrade.needed() is False and upgrade.apply_once() is None
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
    res = upgrade.apply()
    assert res["moved"] == [] and res["marked"] == [] and res["include_removed"] is False
    after = dict(rig.snapshot())
    after.pop(upgrade.STATE_PATH, None)
    assert {k: v for k, v in after.items() if "upgrade-backup" not in k} == before
    assert rig.state["reloads"] == 0                 # nginx was not even reloaded


def test_a_different_answer_after_the_move_puts_everything_back(rig):
    _legacy(rig, "shop", 8080, owner="javahost")
    original = (rig.legacy / "shop.conf").read_text()

    def answer(domain, https):
        if https:
            return None
        moved = (rig.vdir / "javahost_shop.conf").exists()
        return ((301 if moved else 404, None, "text/html"),) * 2
    rig.state["answer"] = answer
    res = upgrade.apply()
    assert res["moved"] == [] and "answered differently" in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").read_text() == original
    assert not (rig.vdir / "javahost_shop.conf").exists()
    assert res["include_removed"] is False and str(rig.legacy) in rig.conf.read_text()


@pytest.mark.parametrize("setup,reason", [
    (lambda r: r.state.update(nginx_ok=False), "config check fails"),
    (lambda r: r.state.update(answer=lambda d, h: None), "nothing could be proven"),
])
def test_no_proof_means_no_move(rig, setup, reason):
    _legacy(rig, "shop", 8080, owner="javahost")
    setup(rig)
    res = upgrade.apply()
    assert res["moved"] == [] and reason in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").exists() and not (rig.vdir / "javahost_shop.conf").exists()


def test_reload_failure_after_the_move_is_undone(rig):
    _legacy(rig, "shop", 8080, owner="javahost")
    calls = {"n": 0}

    def reload_nginx():
        calls["n"] += 1
        return calls["n"] > 1            # the first reload (after the move) fails
    proxy.reload_nginx = reload_nginx
    res = upgrade.apply()
    assert "did not accept" in res["skipped"]["shop"]
    assert (rig.legacy / "shop.conf").exists() and not (rig.vdir / "javahost_shop.conf").exists()


def test_include_stays_while_any_old_file_remains(rig):
    _legacy(rig, "shop", 8080, owner="javahost")
    (rig.legacy / "gone.conf").write_text(LEGACY % dict(app="gone", domain="gone.example.com", port=1))
    res = upgrade.apply()
    assert res["moved"] == ["shop"] and res["include_removed"] is False
    assert (rig.legacy / "gone.conf").exists() and str(rig.legacy) in rig.conf.read_text()


def test_plan_changes_nothing(rig):
    _legacy(rig, "shop", 8080)
    before = dict(rig.snapshot())
    res = upgrade.apply(dry_run=True)
    assert res["dry_run"] and res["moved"] == ["shop"] and res["include_removed"] is True
    assert dict(rig.snapshot()) == before


def test_apply_once_never_raises(rig, monkeypatch):
    monkeypatch.setattr(upgrade, "report", lambda: 1 / 0)
    assert upgrade.apply_once() is None
