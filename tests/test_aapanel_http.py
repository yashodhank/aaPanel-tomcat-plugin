# coding: utf-8
"""aaPanel HTTP compat helpers — AddSite must attach a real reverse-proxy upstream."""
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
    monkeypatch.setattr(
        panel_api, "_AAPANEL_SITE_CONF", str(vhost / "%s.conf"))
    monkeypatch.setattr(
        panel_api, "_AAPANEL_PROXY_DIR", str(vhost / "proxy" / "%s"))
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
