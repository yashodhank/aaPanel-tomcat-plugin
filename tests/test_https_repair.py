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
PLACEHOLDER = ((200, None, "text/html", "placeholder-page", "cert"),) * 2


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
            digest = "placeholder-page" if state.get("same_body") else "app-page"
            return tuple((r[0], r[1], r[2], digest if body else None, "cert") for r in APP)
        return tuple((r[0], r[1], r[2], r[3] if body else None, r[4]) for r in PLACEHOLDER)
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
    (lambda b, m: (b.legacy / "Portal.conf").unlink(), "no site file of its own"),
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


def test_only_the_apps_own_site_file_decides_which_website_is_touched(portal, monkeypatch):
    # the panel note is never consulted, and neither is the marker the app's user can write
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: pytest.fail("not consulted"))
    assert upgrade.https_repair_plan("Portal")["possible"] is True
    portal.app("Other", 8009)
    proxy._store_domain("Other", "portal.example.com")           # what another app's user could write
    plan = upgrade.https_repair_plan("Other")
    assert plan["possible"] is False and "no site file of its own" in plan["reason"]
    assert _by_app(upgrade.report())["Other"]["https_fix"] is None


def test_same_looking_answer_is_not_taken_as_success(portal):
    # an app and a placeholder can share status and content type
    portal.state["same_body"] = True
    res = upgrade.repair_https("Portal")
    assert res["ok"] is False and "cannot be confirmed" in res["error"] and "put back" in res["error"]
    assert portal.rewrite.read_text() == ""


def test_upgrade_headers_only_when_the_map_exists(portal, monkeypatch):
    monkeypatch.setattr(upgrade, "_has_upgrade_map", lambda: False)
    assert upgrade.repair_https("Portal")["ok"]
    assert "$connection_upgrade" not in portal.rewrite.read_text()
    assert upgrade.repair_https("Portal", undo=True)["ok"]
    monkeypatch.setattr(upgrade, "_has_upgrade_map", lambda: True)
    assert upgrade.repair_https("Portal")["ok"]
    assert "proxy_set_header Connection $connection_upgrade;" in portal.rewrite.read_text()


def test_rules_saved_in_the_panel_meanwhile_are_never_overwritten(portal, monkeypatch):
    real = upgrade._stable_answers

    def slow(names, wait):
        out = real(names, wait)
        portal.rewrite.write_text("rewrite ^/a$ /b;\n")          # the admin saves rules mid-check
        return out
    monkeypatch.setattr(upgrade, "_stable_answers", slow)
    res = upgrade.repair_https("Portal")
    assert res["ok"] is False and "changed just now" in res["error"]
    assert portal.rewrite.read_text() == "rewrite ^/a$ /b;\n" and portal.state["reloads"] == 0


def test_undo_refuses_a_block_whose_markers_were_edited_and_flags_a_stale_port(portal):
    assert upgrade.repair_https("Portal")["ok"]
    (portal.tmp / "instances" / "Portal" / "conf" / "server.xml").write_text('<Connector port="8055" />')
    a = _by_app(upgrade.report())["Portal"]
    assert any("no longer uses" in s for s in a["attention"])
    edited = portal.rewrite.read_text().replace(upgrade._FIX_END, "# end")
    portal.rewrite.write_text(edited)
    res = upgrade.repair_https("Portal", undo=True)
    assert res["ok"] is False                                    # nothing recognisable to remove
    assert portal.rewrite.read_text() == edited


def test_app_down_is_said_plainly(portal, monkeypatch):
    monkeypatch.setattr(upgrade, "_direct", lambda port, host: None)
    res = upgrade.repair_https("Portal")
    assert res["ok"] is False and "not answering on port 8001" in res["error"]
    assert portal.rewrite.read_text() == "" and portal.state["reloads"] == 0


def test_default_site_with_a_timestamped_error_page_does_not_block_the_repair(portal, monkeypatch):
    """Seen on a real server: the default site is an app whose JSON 404 carries a
    timestamp, so its body differs on every request. Status and type still decide."""
    real, n = upgrade._probe, {"i": 0}

    def probe(host, https, body=False):
        r = real(host, https, body)
        if host == upgrade._UNKNOWN_HOST and not https and r:
            n["i"] += 1
            return tuple(x[:3] + ("ts-%d" % n["i"],) + x[4:] for x in r)
        return r
    monkeypatch.setattr(upgrade, "_probe", probe)
    assert upgrade.repair_https("Portal")["ok"] is True


def test_changing_bodies_are_dropped_but_any_other_change_still_counts():
    a = {"h": (((404, None, "application/json", "one", None),), None)}
    b = {"h": (((404, None, "application/json", "two", None),), None)}
    assert upgrade._without_changing_bodies(a, b) == {"h": (((404, None, "application/json", None, None),), None)}
    c = {"h": (((500, None, "application/json", "two", None),), None)}
    assert upgrade._without_changing_bodies(a, c) is None
    d = {"h": (((404, None, "application/json", "two", "other-cert"),), None)}
    assert upgrade._without_changing_bodies(a, d) is None
