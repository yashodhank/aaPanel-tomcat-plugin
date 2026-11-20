# coding: utf-8
"""The read-only upgrade check, on layouts older versions left behind."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "javahost"))

from core import config, upgrade  # noqa: E402
from core.compat import aapanel as panel_api  # noqa: E402
from core.deploy import proxy  # noqa: E402
from core.deploy import ssl as sslmod  # noqa: E402
from core.tomcat import instance, service, templating  # noqa: E402

LEGACY = """# Managed by JavaHost — instance %(app)s (%(domain)s). Do not edit by hand.
server {
    listen 80;
    server_name %(domain)s;
    location / {
        proxy_pass http://127.0.0.1:%(port)d;
    }
}
"""
PANEL_SITE = """server
{
    listen 80;
    listen 443 ssl;
    server_name %(domain)s;
    root /www/wwwroot/%(domain)s;
    %(extra)s
}
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    vdir, legacy = tmp_path / "panel-vhost", tmp_path / "legacy-vhost"
    vdir.mkdir(); legacy.mkdir()
    units = tmp_path / "units"; units.mkdir()
    conf = tmp_path / "nginx.conf"
    conf.write_text("http {\n    include %s/*.conf;\n    include %s/*.conf;\n}\n" % (legacy, vdir))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(vdir))
    monkeypatch.setattr(proxy, "LEGACY_VHOST_DIR", str(legacy))
    monkeypatch.setattr(proxy, "NGINX_CONF", str(conf))
    monkeypatch.setattr(panel_api, "_AAPANEL_SITE_CONF", str(vdir / "%s.conf"))
    monkeypatch.setattr(panel_api, "_AAPANEL_PROXY_DIR", str(vdir / "proxy" / "%s"))
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path / "instances"))
    monkeypatch.setattr(service, "SYSTEMD_DIR", str(units))
    monkeypatch.setattr(service, "INITD_DIR", str(tmp_path / "initd"))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))

    def app(name, port, edit=None, java="/www/server/javahost/runtimes/jdk-17"):
        base = tmp_path / "instances" / name
        for d in ("bin", "conf", "webapps/ROOT"):
            (base / d).mkdir(parents=True)
        (base / "conf" / "server.xml").write_text('<Connector port="%d" />' % port)
        unit = templating.render_file("systemd.service.tmpl", service._ctx(
            name, java, "/www/server/javahost/tomcat/10", str(base)))
        if edit:
            unit = unit.replace(*edit)
        (units / ("javahost-%s.service" % name)).write_text(unit)
    snap = {}

    def snapshot():
        snap.clear()
        for root, _, files in os.walk(str(tmp_path)):
            for f in files:
                p = os.path.join(root, f)
                snap[p] = (open(p, "rb").read(), os.stat(p).st_mtime_ns)
        return snap
    return type("Box", (), dict(vdir=vdir, legacy=legacy, conf=conf, app=staticmethod(app),
                                snapshot=staticmethod(snapshot), tmp=tmp_path))


def _by_app(rep):
    return {a["app"]: a for a in rep["apps"]}


def test_the_production_layout_is_reported_and_nothing_is_written(box):
    # Portal: old site file + a panel website without a proxy, HTTPS marked on,
    # no owner marker, service file edited by hand. Acquiring: no domain.
    box.app("Acquiring", 8000)
    box.app("Portal", 8001, edit=("NoNewPrivileges=true", "NoNewPrivileges=false"))
    (box.legacy / "Portal.conf").write_text(LEGACY % dict(app="Portal", domain="portal.example.com", port=8001))
    (box.vdir / "portal.example.com.conf").write_text(PANEL_SITE % dict(domain="portal.example.com", extra=""))
    proxy._store_domain("Portal", "portal.example.com")
    sslmod._mark_ssl("Portal", True)
    before = dict(box.snapshot())

    rep = upgrade.report()
    apps = _by_app(rep)
    assert apps["Acquiring"]["state"] == "not-published"
    assert apps["Acquiring"]["action"] == "none" and not apps["Acquiring"]["attention"]
    assert apps["Acquiring"]["service"] == {"backend": "systemd", "matches_template": True}

    p = apps["Portal"]
    assert p["state"] == "legacy-file-and-panel-website" and p["action"] == "keep"
    assert p["port"] == 8001 and p["domain"] == "portal.example.com"
    text = " ".join(p["attention"])
    assert "leaves both as they are" in text
    assert "HTTPS is marked on, but no HTTPS site" in text and "plain HTTP does reach it" in text
    assert p["service"]["matches_template"] is False
    assert any("differs from the current template" in n for n in p["notes"])

    assert rep["legacy_include"] is True and rep["legacy_files"] == ["Portal.conf"]
    assert "stays while old site files remain (Portal)" in rep["server"][0]
    assert rep["counts"] == {"apps": 2, "to_move": 0, "attention": 1}
    assert dict(box.snapshot()) == before            # read-only, byte for byte


def test_old_site_file_alone_is_marked_to_move(box):
    box.app("shop", 8080)
    (box.legacy / "shop.conf").write_text(LEGACY % dict(app="shop", domain="shop.example.com", port=8080))
    proxy._store_domain("shop", "shop.example.com")
    proxy._store_owner("shop", "javahost")
    a = _by_app(upgrade.report())["shop"]
    assert a["state"] == "legacy-file" and a["action"] == "move" and not a["attention"]
    assert "removed once the old site files have moved" in upgrade.report()["server"][0]


def test_panel_website_is_checked_for_a_real_proxy(box):
    box.app("api", 8090)
    proxy._store_domain("api", "api.example.com")
    site = box.vdir / "api.example.com.conf"
    site.write_text(PANEL_SITE % dict(domain="api.example.com", extra=""))
    a = _by_app(upgrade.report())["api"]
    assert a["state"] == "panel-website" and a["action"] == "mark"
    assert "does not send traffic to this app" in a["attention"][0]
    site.write_text(PANEL_SITE % dict(
        domain="api.example.com", extra="location / { proxy_pass http://127.0.0.1:8090; }"))
    a = _by_app(upgrade.report())["api"]
    assert a["state"] == "panel-website" and not a["attention"]


def test_current_layout_is_clean(box):
    box.app("shop", 8080)
    (box.vdir / "javahost_shop.conf").write_text(LEGACY % dict(app="shop", domain="shop.example.com", port=8080))
    proxy._store_domain("shop", "shop.example.com")
    proxy._store_owner("shop", "javahost")
    box.conf.write_text("http {\n    include %s/*.conf;\n}\n" % box.vdir)
    rep = upgrade.report()
    a = _by_app(rep)["shop"]
    assert a["state"] == "site-file" and a["action"] == "none" and not a["attention"] and not a["notes"]
    assert rep["legacy_include"] is False and rep["server"] == []


def test_shared_panel_java_and_orphan_files_are_flagged(box):
    box.app("old", 8070, java="/usr/local/btjdk/jdk8")
    (box.legacy / "gone.conf").write_text(LEGACY % dict(app="gone", domain="gone.example.com", port=1))
    rep = upgrade.report()
    assert "panel's shared Java" in _by_app(rep)["old"]["attention"][0]
    assert any("no matching app are left alone: gone.conf" in s for s in rep["server"])


def test_unit_template_match_ignores_values_but_not_edits():
    tmpl = "User=@@user@@\nA=@@base@@/x\nB=@@base@@/y\nN=true\n"
    assert upgrade.unit_matches_template("User=www\nA=/i/app/x\nB=/i/app/y\nN=true\n", tmpl)
    assert not upgrade.unit_matches_template("User=www\nA=/i/app/x\nB=/i/app/y\nN=false\n", tmpl)
    assert not upgrade.unit_matches_template("User=www\nA=/i/app/x\nB=/other/y\nN=true\n", tmpl)


def test_owner_marker_is_never_followed_or_echoed(box):
    box.app("shop", 8080)
    secret = box.tmp / "secret"; secret.write_text("root:hash\n")
    marker = box.tmp / "instances" / "shop" / "bin" / "site.owner"
    os.symlink(str(secret), str(marker))
    assert _by_app(upgrade.report())["shop"]["owner_marker"] is None
    marker.unlink(); marker.write_text("something else entirely\n")
    assert _by_app(upgrade.report())["shop"]["owner_marker"] is None
    marker.write_text("javahost\n")
    assert _by_app(upgrade.report())["shop"]["owner_marker"] == "javahost"


def test_nothing_shared_is_ever_marked_to_move(box):
    def legacy(app, port, names):
        box.app(app, port)
        (box.legacy / ("%s.conf" % app)).write_text(
            (LEGACY % dict(app=app, domain="x", port=port)).replace("server_name x;", "server_name %s;" % names))
    # an alias of the old file is a panel website
    legacy("a", 8001, "a.example.com www.a.example.com")
    (box.vdir / "www.a.example.com.conf").write_text(PANEL_SITE % dict(domain="www.a.example.com", extra=""))
    # two old files name one domain
    legacy("b", 8002, "same.example.com"); legacy("c", 8003, "same.example.com")
    # old and current file for one app
    legacy("d", 8004, "d.example.com")
    (box.vdir / "javahost_d.conf").write_text(LEGACY % dict(app="d", domain="d.example.com", port=8004))
    # regex name
    legacy("e", 8005, "~^e\\d+\\.example\\.com$")
    # marker points at another app's domain
    legacy("f", 8006, "f.example.com"); proxy._store_domain("f", "same.example.com")
    apps = _by_app(upgrade.report())
    for name in "abcde":
        assert apps[name]["action"] == "keep" and apps[name]["attention"], name
    assert apps["f"]["action"] == "keep" and apps["f"]["domain"] == "f.example.com"
    assert "is not the one in the app's site file" in apps["f"]["attention"][0]


def test_old_folder_that_nginx_no_longer_loads_is_not_moved(box):
    box.app("shop", 8080)
    (box.legacy / "shop.conf").write_text(LEGACY % dict(app="shop", domain="shop.example.com", port=8080))
    box.conf.write_text("http {\n    # include %s/*.conf;\n    include %s/*.conf;\n}\n" % (box.legacy, box.vdir))
    a = _by_app(upgrade.report())["shop"]
    assert a["action"] == "keep" and "not being served" in a["attention"][0]
