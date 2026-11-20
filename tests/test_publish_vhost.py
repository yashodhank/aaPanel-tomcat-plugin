# coding: utf-8
"""Publishing a domain as a JavaHost-owned vhost (the default publish mode)."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "javahost"))

import javahost_main  # noqa: E402
from core import config  # noqa: E402
from core.compat import aapanel as panel_api  # noqa: E402
from core.deploy import proxy  # noqa: E402
from core.deploy import ssl as sslmod  # noqa: E402
from core.tomcat import instance  # noqa: E402


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fake server: panel vhost dir, legacy dir, nginx.conf, two apps."""
    vdir, legacy = tmp_path / "panel-vhost", tmp_path / "legacy-vhost"
    vdir.mkdir(); legacy.mkdir()
    conf = tmp_path / "nginx.conf"
    conf.write_text("http {\n    include %s/*.conf;\n}\n" % vdir)
    monkeypatch.setattr(proxy, "VHOST_DIR", str(vdir))
    monkeypatch.setattr(proxy, "LEGACY_VHOST_DIR", str(legacy))
    monkeypatch.setattr(proxy, "NGINX_CONF", str(conf))
    monkeypatch.setattr(proxy, "ensure_include",
                        lambda c=None, _f=proxy.ensure_include: _f(str(conf)))
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path / "instances"))
    for app in ("shop", "blog"):
        (tmp_path / "instances" / app / "bin").mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    monkeypatch.setattr(panel_api, "ensure_ws_map", lambda: True)
    state = SimpleNamespace(vdir=vdir, legacy=legacy, conf=conf, reloads=0, nginx_ok=True)

    def reload_nginx():
        state.reloads += 1
        return state.nginx_ok
    monkeypatch.setattr(proxy, "reload_nginx", reload_nginx)
    # the panel must never be asked in vhost mode
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda *a: pytest.fail("vhost mode called the panel"))
    return state


def test_default_mode_publishes_without_the_panel_or_a_key(box):
    assert config.publish_mode() == "vhost" and not config.aapanel_api_key()
    res = proxy.set_site("shop", "shop.example.com", 8080)
    assert res == {"ok": True, "domain": "shop.example.com",
                   "url": "http://shop.example.com/", "via": "vhost", "owner": "javahost"}
    body = (box.vdir / "javahost_shop.conf").read_text()
    assert "server_name shop.example.com;" in body
    assert "proxy_pass http://127.0.0.1:8080;" in body
    assert proxy.read_domain("shop") == "shop.example.com"
    assert proxy.read_owner("shop") == "javahost"
    assert box.reloads == 1
    # nginx.conf is left exactly as the panel wrote it
    assert box.conf.read_text() == "http {\n    include %s/*.conf;\n}\n" % box.vdir


def test_rejected_config_is_rolled_back(box):
    box.nginx_ok = False
    res = proxy.set_site("shop", "shop.example.com", 8080)
    assert res["ok"] is False and "nginx rejected" in res["error"]
    assert os.listdir(str(box.vdir)) == []          # nothing left to break later reloads
    assert proxy.read_domain("shop") is None


def test_rejected_change_restores_the_previous_site(box):
    proxy.set_site("shop", "shop.example.com", 8080)
    before = (box.vdir / "javahost_shop.conf").read_text()
    box.nginx_ok = False
    res = proxy.set_site("shop", "new.example.com", 8080)
    assert res["ok"] is False
    assert (box.vdir / "javahost_shop.conf").read_text() == before
    assert proxy.read_domain("shop") == "shop.example.com"


def test_missing_panel_include_is_an_error_not_an_nginx_conf_edit(box):
    box.conf.write_text("http {\n}\n")
    res = proxy.set_site("shop", "shop.example.com", 8080)
    assert res["ok"] is False and "does not load" in res["error"]
    assert box.conf.read_text() == "http {\n}\n"
    assert os.listdir(str(box.vdir)) == []


def test_domain_of_a_panel_website_is_refused(box):
    (box.vdir / "shop.example.com.conf").write_text("server { server_name shop.example.com; }\n")
    res = proxy.set_site("shop", "shop.example.com", 8080)
    assert res["ok"] is False and "already a website in the panel" in res["error"]
    assert not (box.vdir / "javahost_shop.conf").exists()


def test_domain_of_another_app_is_refused(box):
    assert proxy.set_site("shop", "shop.example.com", 8080)["ok"]
    res = proxy.set_site("blog", "shop.example.com", 8081)
    assert res["ok"] is False and "'shop'" in res["error"]
    # a different host that merely ends the same is fine
    assert proxy.set_site("blog", "my-shop.example.com", 8081)["ok"]


def test_legacy_vhost_moves_to_the_panel_dir_on_republish(box):
    (box.legacy / "shop.conf").write_text("server { server_name shop.example.com; }\n")
    assert proxy.vhost_apps() == ["shop"]
    assert proxy.set_site("shop", "shop.example.com", 8080)["ok"]
    assert not (box.legacy / "shop.conf").exists()
    assert (box.vdir / "javahost_shop.conf").exists()
    assert proxy.vhost_apps() == ["shop"]


def test_other_files_in_the_panel_dir_are_not_ours(box):
    (box.vdir / "0.websocket.conf").write_text("")
    (box.vdir / "site.example.com.conf").write_text("")
    assert proxy.vhost_apps() == []


def test_remove_site_drops_the_vhost_without_asking_the_panel(box, monkeypatch):
    proxy.set_site("shop", "shop.example.com", 8080)
    monkeypatch.setattr(proxy, "aapanel_remove_site",
                        lambda d: pytest.fail("no panel site exists to remove"))
    monkeypatch.setattr(panel_api, "site_ownership",
                        lambda d: pytest.fail("no panel site exists to look up"))
    res = proxy.remove_site("shop")
    assert res["removed"] is True
    assert os.listdir(str(box.vdir)) == []
    assert proxy.read_domain("shop") is None


def test_republishing_turns_the_https_marker_off(box):
    proxy.set_site("shop", "shop.example.com", 8080)
    sslmod._mark_ssl("shop", True)
    proxy.set_site("shop", "new.example.com", 8080)
    assert sslmod.read_ssl("shop") is False       # the new vhost is plain HTTP


def test_panel_mode_needs_a_key_and_clearing_the_key_falls_back(box):
    m = javahost_main.javahost_main()
    r = m.SetPanelApi(SimpleNamespace(publish_mode="panel"))
    assert r["status"] is False and "API key" in r["msg"]
    r = m.SetPanelApi(SimpleNamespace(publish_mode="panel", api_key="k" * 32))
    assert r["status"] is True and config.publish_mode() == "panel"
    assert m.SetPanelApi(SimpleNamespace(publish_mode="bogus"))["status"] is False
    r = m.SetPanelApi(SimpleNamespace(api_key=""))
    assert r["status"] is True and config.publish_mode() == "vhost"
    assert m.GetPanelApi()["msg"]["publish_mode"] == "vhost"


def test_https_toggle_cannot_take_a_domain_someone_else_serves(box):
    # the domain SetSiteSSL works from is read from a file the app user can write
    (box.vdir / "bank.example.com.conf").write_text("server { server_name bank.example.com; }\n")
    err = sslmod._activate_plugin_vhost("shop", "bank.example.com", 8080, ssl_on=False)
    assert err and "already a website in the panel" in err
    assert not (box.vdir / "javahost_shop.conf").exists() and box.reloads == 0


def test_previous_panel_website_is_reported_not_deleted(box, monkeypatch):
    proxy._store_domain("shop", "old.example.com")       # no owner marker => panel-owned
    monkeypatch.setattr(proxy, "aapanel_remove_site",
                        lambda d: pytest.fail("must not delete a panel website"))
    res = proxy.set_site("shop", "new.example.com", 8080)
    assert res["ok"] and "old.example.com was left in place" in res["note"]


def test_upgrade_map_shipped_by_the_panel_means_no_nginx_conf_edit(tmp_path, monkeypatch):
    vdir = tmp_path / "vhost"; vdir.mkdir()
    conf = tmp_path / "nginx.conf"; conf.write_text("http {\n}\n")
    assert panel_api._ws_map_in_vhost_dir(str(vdir)) is False
    (vdir / "0.websocket.conf").write_text(
        "map $http_upgrade $connection_upgrade {\n    default upgrade;\n    ''  close;\n}")
    assert panel_api._ws_map_in_vhost_dir(str(vdir)) is True
    monkeypatch.setattr(panel_api, "_ws_map_in_vhost_dir", lambda: True)
    assert panel_api.ensure_ws_map(str(conf)) is True
    assert conf.read_text() == "http {\n}\n"
