# coding: utf-8
"""aaPanel HTTP compat helpers — AddSite must attach a real reverse-proxy upstream."""
import json

from core.compat import aapanel as panel_api
from core import config


def test_http_add_site_with_proxy_calls_createproxy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")
    config.set("aapanel_port", 37778)

    calls = []

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        calls.append({"path": path, "params": dict(params or {}),
                      "body": dict(body or {}), "method": method})
        action = (params or {}).get("action")
        if action == "AddSite":
            return {"status": True, "msg": "ok"}, None
        if action == "CreateProxy":
            return {"status": True, "msg": "ok"}, None
        return {"status": False}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    monkeypatch.setattr(panel_api.os, "makedirs", lambda *a, **k: None)
    res = panel_api.http_add_site_with_proxy("app.example.com", 8085)
    assert res and res["ok"] is True
    assert "CreateProxy" in res["detail"]
    assert any(c["params"].get("action") == "AddSite" for c in calls)
    proxy_calls = [c for c in calls if c["params"].get("action") == "CreateProxy"]
    assert len(proxy_calls) == 1
    assert proxy_calls[0]["body"]["proxysite"] == "http://127.0.0.1:8085"
    assert proxy_calls[0]["body"]["sitename"] == "app.example.com"
    assert proxy_calls[0]["body"]["type"] == "1"
    assert proxy_calls[0]["body"]["todomain"] == "$host"


def test_http_add_site_fails_without_proxy_attach(monkeypatch, tmp_path):
    """Site row alone is not success — CreateProxy + file fallback must both fail."""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        action = (params or {}).get("action")
        if action == "AddSite":
            return {"status": True}, None
        return {"status": False, "msg": "nope"}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    monkeypatch.setattr(panel_api.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(panel_api.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        panel_api, "write_aapanel_proxy_files",
        lambda domain, port: (False, "nginx -t failed"))
    res = panel_api.http_add_site_with_proxy("app.example.com", 8085)
    assert isinstance(res, dict) and res.get("ok") is False
    assert "CreateProxy failed" in res.get("error", "")


def test_http_add_site_uses_proxy_file_fallback(monkeypatch, tmp_path):
    """When CreateProxy fails, writing aaPanel proxy include files still succeeds."""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        action = (params or {}).get("action")
        if action == "AddSite":
            return {"status": True}, None
        return {"status": False, "msg": "location not allowed"}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    monkeypatch.setattr(panel_api.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(panel_api.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        panel_api, "write_aapanel_proxy_files",
        lambda domain, port: (True, ""))
    res = panel_api.http_add_site_with_proxy("app.example.com", 8085)
    assert res and res["ok"] is True
    assert res["path"] == "aapanel-http-fallback"
    assert "proxy files" in res["detail"]


def test_write_aapanel_proxy_files_layout(monkeypatch, tmp_path):
    domain = "jhprobe.example.com"
    vhost = tmp_path / "vhost" / "nginx"
    proxy_root = vhost / "proxy" / domain
    site_conf = vhost / ("%s.conf" % domain)
    site_conf.parent.mkdir(parents=True)
    site_conf.write_text(
        "server\n{\n    listen 80;\n    server_name %s;\n"
        "    #REWRITE-START\n    include /tmp/rw.conf;\n    #REWRITE-END\n"
        "    #PROXY-START/\nlocation /\n{\n    proxy_pass http://bad;\n}\n"
        "#PROXY-END/\n"
        "    #error_page 404 /404.html;\n}\n" % domain
    )
    proxyfile = tmp_path / "data" / "proxyfile.json"
    proxyfile.parent.mkdir(parents=True)
    proxyfile.write_text("[]")
    monkeypatch.setattr(
        panel_api, "_AAPANEL_SITE_CONF", str(vhost / "%s.conf"))
    monkeypatch.setattr(
        panel_api, "_AAPANEL_PROXY_DIR", str(vhost / "proxy" / "%s"))
    monkeypatch.setattr(panel_api, "_AAPANEL_PROXYFILE", str(proxyfile))
    monkeypatch.setattr(
        panel_api, "run",
        lambda argv, check=False, timeout=15: (0, "syntax is ok", ""))

    ok, err = panel_api.write_aapanel_proxy_files(domain, 18080)
    assert ok is True, err
    assert err == ""
    body = site_conf.read_text()
    assert "include %s/*.conf;" % proxy_root in body
    assert "#REWRITE-END" in body
    # Inline broken CreateProxy block must be scrubbed from the site conf.
    assert "#PROXY-START/" not in body
    assert "proxy_pass http://bad" not in body
    confs = list(proxy_root.glob("*.conf"))
    assert len(confs) == 1
    proxy_txt = confs[0].read_text()
    assert "proxy_pass http://127.0.0.1:18080;" in proxy_txt
    assert "#PROXY-START/" in proxy_txt
    assert "Upgrade $http_upgrade" in proxy_txt
    rows = json.loads(proxyfile.read_text())
    assert len(rows) == 1
    assert rows[0]["sitename"] == domain
    assert rows[0]["proxysite"] == "http://127.0.0.1:18080"
    assert rows[0]["proxyname"].startswith("javahost-")


def test_register_proxyfile_upserts(monkeypatch, tmp_path):
    path = tmp_path / "proxyfile.json"
    path.write_text(json.dumps([{
        "proxyname": "javahost-old",
        "sitename": "app.example.com",
        "proxydir": "/",
        "proxysite": "http://127.0.0.1:1",
    }, {
        "proxyname": "other",
        "sitename": "other.example.com",
        "proxydir": "/",
        "proxysite": "http://127.0.0.1:9",
    }]))
    monkeypatch.setattr(panel_api, "_AAPANEL_PROXYFILE", str(path))
    ok, err = panel_api._register_proxyfile("app.example.com", 8085)
    assert ok is True, err
    rows = json.loads(path.read_text())
    assert len(rows) == 2
    by_site = {r["sitename"]: r for r in rows}
    assert by_site["app.example.com"]["proxysite"] == "http://127.0.0.1:8085"
    assert by_site["other.example.com"]["proxysite"] == "http://127.0.0.1:9"


def test_register_proxyfile_rejects_non_list(monkeypatch, tmp_path):
    path = tmp_path / "proxyfile.json"
    path.write_text("{}")
    monkeypatch.setattr(panel_api, "_AAPANEL_PROXYFILE", str(path))
    ok, err = panel_api._register_proxyfile("app.example.com", 8085)
    assert ok is False
    assert "shape" in err
    assert path.read_text() == "{}"  # unchanged


def test_detect_panel_port_reads_port_pl(tmp_path, monkeypatch):
    pl = tmp_path / "port.pl"
    pl.write_text("8888")
    monkeypatch.setattr(panel_api, "_PORT_PL", str(pl))
    monkeypatch.setattr(config, "get", lambda key, default=None: None)
    assert panel_api.detect_panel_port() == 8888


def test_set_panel_api_persists_key(tmp_path, monkeypatch):
    import javahost_main
    from types import SimpleNamespace
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    api = javahost_main.javahost_main()
    res = api.SetPanelApi(SimpleNamespace(api_key="secret-key", port="12345"))
    assert res["status"] is True
    assert res["msg"]["aapanel_api_key_set"] is True
    assert res["msg"]["aapanel_port"] == 12345
    assert config.aapanel_api_key() == "secret-key"
    # Get never echoes the secret
    got = api.GetPanelApi(SimpleNamespace())
    assert got["msg"]["aapanel_api_key_set"] is True
    assert "api_key" not in got["msg"] or got["msg"].get("api_key") in (None, "")


def test_require_nginx_fails_closed_for_apache(monkeypatch):
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "apache")
    err = panel_api.require_nginx()
    assert err and "nginx" in err.lower() and "apache" in err.lower()


def test_require_nginx_ok_for_nginx(monkeypatch):
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    assert panel_api.require_nginx() is None


def test_http_add_site_fails_closed_when_not_nginx(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "openlitespeed")
    calls = []

    def fake_http(*a, **k):
        calls.append(1)
        return {"status": True}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    res = panel_api.http_add_site_with_proxy("app.example.com", 8085)
    assert isinstance(res, dict) and res.get("ok") is False
    assert "nginx" in res.get("error", "").lower()
    assert calls == []  # never hit panel API


def test_http_enable_site_ssl_ignores_cert_only(monkeypatch, tmp_path):
    """apply_cert success alone must not count as HTTPS enabled."""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        action = (params or {}).get("action")
        if action == "apply_cert_api":
            return {"status": True, "cert": "ok"}, None
        # SetSSL / HttpToHttps / set_https_mode all fail
        return {"status": False, "msg": "nope"}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    assert panel_api.http_enable_site_ssl("app.example.com") is False


def test_http_enable_site_ssl_requires_https_action(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        action = (params or {}).get("action")
        if action == "apply_cert_api":
            return {"status": False}, None
        if action == "HttpToHttps":
            return {"status": True}, None
        return {"status": False}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    assert panel_api.http_enable_site_ssl("app.example.com") is True


def test_http_add_site_conflict_on_foreign_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        action = (params or {}).get("action")
        if action == "AddSite":
            return {"status": False, "msg": "Site already exists"}, None
        if action == "getData":
            return {"data": [{"id": 9, "name": "app.example.com",
                             "ps": "PHP site"}]}, None
        return {"status": False}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    monkeypatch.setattr(panel_api.os, "makedirs", lambda *a, **k: None)
    res = panel_api.http_add_site_with_proxy("app.example.com", 8085)
    assert isinstance(res, dict) and res.get("ok") is False
    assert res.get("conflict") is True
    assert "not a JavaHost site" in res.get("error", "")


def test_http_add_site_rolls_back_orphan(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "test-sk")
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    deleted = []

    def fake_http(path, params=None, body=None, method="GET", timeout=30):
        action = (params or {}).get("action")
        if action == "AddSite":
            return {"status": True}, None
        if action == "getData":
            return {"data": [{"id": 42, "name": "app.example.com",
                             "ps": "JavaHost: app"}]}, None
        if action == "DeleteSite":
            deleted.append(body)
            return {"status": True}, None
        return {"status": False, "msg": "CreateProxy crash"}, None

    monkeypatch.setattr(panel_api, "http_request", fake_http)
    monkeypatch.setattr(panel_api.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(panel_api.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(panel_api, "scrub_site_inline_proxy",
                        lambda domain: (False, ""))
    monkeypatch.setattr(panel_api, "_restore_site_conf", lambda *a, **k: None)
    monkeypatch.setattr(
        panel_api, "write_aapanel_proxy_files",
        lambda domain, port: (False, "nginx -t failed"))
    res = panel_api.http_add_site_with_proxy("app.example.com", 8085)
    assert isinstance(res, dict) and res.get("ok") is False
    assert res.get("rolled_back") is True
    assert "rolled back" in res.get("error", "")
    assert deleted and deleted[0].get("id") == "42"
