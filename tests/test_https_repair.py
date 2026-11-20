# coding: utf-8
"""HTTPS repair for an app whose panel website has TLS but no proxy."""
import os

import pytest

from test_upgrade_report import LEGACY, box, _by_app  # noqa: F401

from core import upgrade
from core.compat import aapanel as panel_api
from core.deploy import proxy
from core.deploy import ssl as sslmod

SITE = """server
{
    listen 80;
    listen 443 ssl;
    server_name portal.example.com;
    root /www/wwwroot/portal.example.com;
    if ($server_port !~ 443){
        rewrite ^(/.*)$ https://$host$1 permanent;
    }
    include %(rewrite)s;
    location ~ \\.well-known{
        allow all;
    }
}
"""
APP = ((404, None, "application/json", None, None),) * 2
PLACEHOLDER = ((200, None, "text/html", None, "cert"),) * 2


@pytest.fixture
def portal(box, monkeypatch):
    rw = box.tmp / "rewrite"; rw.mkdir()
    monkeypatch.setattr(panel_api, "_AAPANEL_REWRITE", str(rw / "%s.conf"))
    monkeypatch.setattr(upgrade, "BACKUP_ROOT", str(box.tmp / "upgrade-backup"))
    monkeypatch.setattr(upgrade, "_SETTLE", (0, 0))
    monkeypatch.setattr(upgrade, "_BEFORE", (0, 0))
    box.app("Portal", 8001)
    (box.legacy / "Portal.conf").write_text(LEGACY % dict(app="Portal", domain="portal.example.com", port=8001))
    box.rewrite = rw / "portal.example.com.conf"
    box.rewrite.write_text("")
    box.site = box.vdir / "portal.example.com.conf"
    box.site.write_text(SITE % dict(rewrite=box.rewrite))
    proxy._store_domain("Portal", "portal.example.com")
    sslmod._mark_ssl("Portal", True)
    state = {"reloads": 0, "nginx_ok": True, "http_breaks": False, "https_wrong": False}
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: "javahost")
    monkeypatch.setattr(proxy, "nginx_test", lambda: state["nginx_ok"])

    def reload_nginx():
        state["reloads"] += 1
        return state["nginx_ok"]
    monkeypatch.setattr(proxy, "reload_nginx", reload_nginx)

    def probe(host, https, body=False):
        fixed = "JavaHost HTTPS proxy" in box.rewrite.read_text()
        if not https:
            return ((301, None, "text/html", None, None),) * 2 if (fixed and state["http_breaks"]) else APP
        if fixed and not state["https_wrong"]:
            return tuple((r[0], r[1], r[2], None, "cert") for r in APP)
        return PLACEHOLDER
    monkeypatch.setattr(upgrade, "_probe", probe)
    monkeypatch.setattr(upgrade, "_direct", lambda port, host: tuple((r[0], r[2]) for r in APP))
    box.state = state
    return box


def test_report_offers_the_repair_only_for_the_broken_case(portal):
    a = _by_app(upgrade.report())["Portal"]
    assert a["https_fix"]["possible"] is True and "keeps answering exactly" in a["https_fix"]["effect"]
    assert any("HTTPS is marked on" in s for s in a["attention"])


def test_repair_adds_one_block_and_undo_removes_it(portal):
    site_before = portal.site.read_bytes()
    legacy_before = (portal.legacy / "Portal.conf").read_bytes()
    res = upgrade.repair_https("Portal")
    assert res["ok"] is True and res["url"] == "https://portal.example.com/"
    text = portal.rewrite.read_text()
    assert "location ^~ / {" in text and "proxy_pass http://127.0.0.1:8001;" in text
    assert portal.site.read_bytes() == site_before                   # the panel's file is not edited
    assert (portal.legacy / "Portal.conf").read_bytes() == legacy_before
    assert open(res["backup"], "rb").read() == b""
    a = _by_app(upgrade.report())["Portal"]
    assert a["https_fix"]["applied"] is True
    assert not any("HTTPS is marked on" in s for s in a["attention"])     # the warning is gone
    assert upgrade.repair_https("Portal")["ok"] is False                  # not applied twice
    res = upgrade.repair_https("Portal", undo=True)
    assert res["ok"] and res["undone"] and portal.rewrite.read_text() == ""
    assert upgrade.repair_https("Portal", undo=True)["ok"] is False


@pytest.mark.parametrize("flag,why", [("http_breaks", "plain HTTP answered differently"),
                                      ("https_wrong", "still does not answer what the app answers")])
def test_a_wrong_result_puts_the_file_back(portal, flag, why):
    portal.state[flag] = True
    res = upgrade.repair_https("Portal")
    assert res["ok"] is False and why in res["error"] and "put back" in res["error"]
    assert portal.rewrite.read_text() == ""
    assert portal.state["reloads"] == 2                 # the change, then the restore


@pytest.mark.parametrize("setup,reason", [
    (lambda b, m: (m.setattr(panel_api, "site_ownership", lambda d: "foreign"),
                   (b.legacy / "Portal.conf").unlink()), "not created by JavaHost"),
    (lambda b, m: b.rewrite.write_text("rewrite ^/old$ /new permanent;\n"), "already has URL rewrite rules"),
    (lambda b, m: b.site.write_text(b.site.read_text().replace("    listen 443 ssl;\n", "")), "no HTTPS yet"),
    (lambda b, m: b.site.write_text(b.site.read_text().replace("include", "#include")), "does not load"),
    (lambda b, m: b.site.write_text(b.site.read_text().replace("location ~ \\.well-known{", "location / { return 200; }\n    location ~ \\.well-known{")), "already has a location"),
    (lambda b, m: b.state.update(nginx_ok=False), "config check fails"),
])
def test_repair_refuses_anything_it_does_not_fully_understand(portal, monkeypatch, setup, reason):
    setup(portal, monkeypatch)
    before = portal.rewrite.read_bytes()
    res = upgrade.repair_https("Portal")
    assert res["ok"] is False and reason in res["error"]
    assert portal.rewrite.read_bytes() == before and portal.state["reloads"] == 0


def test_endpoint_reports_refusals_as_errors(portal):
    import javahost_main
    from types import SimpleNamespace as NS
    portal.rewrite.write_text("rewrite ^/a$ /b;\n")
    r = javahost_main.javahost_main().RepairHttps(NS(app="Portal"))
    assert r["status"] is False and "URL rewrite rules" in r["msg"]


def test_old_style_panel_note_is_fine_when_the_apps_own_file_serves_the_name(portal, monkeypatch):
    # the layout older versions left: a note the strict marker check calls foreign
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: "foreign")
    assert upgrade.https_repair_plan("Portal")["possible"] is True
