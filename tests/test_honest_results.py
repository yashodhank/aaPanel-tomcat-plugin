# coding: utf-8
"""An operation that did not happen must not be reported as done. Covers the
paths that used to return a success envelope around a failure, or silently
replace what the operator asked for with something else."""
import json
import os
import threading
import types

import pytest

from core import config, maintenance, updates
from core.compat import aapanel as panel_api
from core.db import engines as dbengines
from core.db import _base as dbbase
from core.deploy import proxy, ssl
from core.runtime import java
from core.tomcat import installer, instance, registry
from core.util import fs


# ---- settings ---------------------------------------------------------------- #
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.clear()
    return tmp_path / "config.json"


def test_a_saved_zero_is_kept(cfg):
    config.update({"log_rotate_keep": 0, "log_purge_days": 0})
    assert config.log_rotate_keep() == 0        # was 7
    assert config.log_purge_days() == 0         # was 30
    config.update({"log_rotate_max_mb": 0})
    assert config.log_rotate_max_mb() == 1      # floor still applies


def test_unset_and_junk_fall_back_to_defaults(cfg):
    assert (config.log_rotate_keep(), config.log_purge_days(), config.log_rotate_max_mb()) == (7, 30, 50)
    config.update({"log_rotate_keep": "x", "log_purge_days": None})
    assert (config.log_rotate_keep(), config.log_purge_days()) == (7, 30)


def test_an_unreadable_config_is_set_aside_not_overwritten(cfg):
    cfg.write_text('{"aapanel_api_key": "KEEP-ME", "site_suffix": "example.com"')   # truncated
    config.update({"log_rotate_keep": 3})       # saving still works
    assert json.loads(cfg.read_text()) == {"log_rotate_keep": 3}
    aside = cfg.with_name("config.json.unreadable")
    assert "KEEP-ME" in aside.read_text()       # what was there is kept for the operator
    assert os.stat(str(aside)).st_mode & 0o777 == 0o600


def test_an_empty_config_file_is_just_empty(cfg):
    cfg.write_text("")
    config.update({"log_rotate_keep": 3})
    assert json.loads(cfg.read_text()) == {"log_rotate_keep": 3}
    assert not cfg.with_name("config.json.unreadable").exists()


def test_concurrent_updates_do_not_lose_keys(cfg):
    errors = []

    def save(i):
        try:
            config.update({"k%d" % i: i})
        except Exception as e:       # the old shared temp file raised here
            errors.append(e)
    threads = [threading.Thread(target=save, args=(i,)) for i in range(24)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    data = json.loads(cfg.read_text())
    assert sorted(data) == sorted("k%d" % i for i in range(24))
    assert os.stat(str(cfg)).st_mode & 0o777 == 0o600


# ---- DeleteApp --------------------------------------------------------------- #
@pytest.fixture
def blocked_app(tmp_path, monkeypatch):
    """An app whose aaPanel site cannot be removed right now."""
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path / "instances"))
    monkeypatch.setattr(fs, "MANAGED_ROOTS", tuple(fs.MANAGED_ROOTS) + (str(tmp_path),))
    base = tmp_path / "instances" / "demo"
    (base / "bin").mkdir(parents=True)
    fs.mark_managed(str(base))
    monkeypatch.setattr(proxy, "remove_site", lambda app: {
        "app": app, "removed": False, "domain": "demo.example.com",
        "error": "aaPanel site delete did not confirm removal"})
    touched = []
    monkeypatch.setattr(instance.service, "remove_unit", lambda app: touched.append("unit"))
    from core.backup import schedule as backupschedule
    monkeypatch.setattr(backupschedule, "remove_schedule",
                        lambda app: touched.append("schedule") or {"removed": True})
    return base, touched


def test_blocked_delete_leaves_the_app_whole(blocked_app):
    base, touched = blocked_app
    out = instance.delete("demo")
    assert out["removed"] is False and base.is_dir()
    assert touched == []            # not stopped, unit and schedule still there
    assert "retried" in out["site_warning"]


def test_delete_app_endpoint_reports_a_blocked_delete_as_an_error(blocked_app):
    import javahost_main
    res = javahost_main.javahost_main().DeleteApp(types.SimpleNamespace(app="demo"))
    assert res.get("status") is False
    assert "was not deleted" in res["msg"] and "did not confirm removal" in res["msg"]


def test_force_deletes_the_app_and_says_the_site_may_remain(blocked_app):
    base, touched = blocked_app
    import javahost_main
    res = javahost_main.javahost_main().DeleteApp(types.SimpleNamespace(app="demo", force="1"))
    assert res.get("status") is True and not base.exists()
    assert sorted(touched) == ["schedule", "unit"]
    body = res.get("msg") if isinstance(res.get("msg"), dict) else res
    assert "did not confirm removal" in str(body)       # the leftover is still reported


def test_deleting_an_app_whose_directory_is_already_gone_succeeds(blocked_app, monkeypatch):
    base, touched = blocked_app
    monkeypatch.setattr(proxy, "remove_site", lambda app: {"app": app, "removed": True, "domain": None})
    import shutil
    shutil.rmtree(str(base))
    import javahost_main
    res = javahost_main.javahost_main().DeleteApp(types.SimpleNamespace(app="demo"))
    assert res.get("status") is True        # a second click, or a leftover unit: nothing left to delete


def test_delete_app_still_deletes(blocked_app, monkeypatch):
    base, touched = blocked_app
    monkeypatch.setattr(proxy, "remove_site", lambda app: {"app": app, "removed": True, "domain": None})
    import javahost_main
    res = javahost_main.javahost_main().DeleteApp(types.SimpleNamespace(app="demo"))
    assert res.get("status") is True and not base.exists()
    assert sorted(touched) == ["schedule", "unit"]


# ---- SSL enable -------------------------------------------------------------- #
@pytest.fixture
def aapanel_site(tmp_path, monkeypatch):
    """An aaPanel-owned site with a certificate already on disk."""
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "ACME_WEBROOT", str(tmp_path / "acme"))
    (tmp_path / "site" / "bin").mkdir(parents=True)
    proxy._store_domain("site", "app.example.com")
    proxy._store_owner("site", "aapanel")
    monkeypatch.setattr(panel_api, "api_key_configured", lambda: False)
    monkeypatch.setattr(ssl, "_find_wildcard_cert", lambda d: (None, None))
    monkeypatch.setattr(ssl, "_install_renewal_hook", lambda: None)
    monkeypatch.setattr(ssl.shell, "which", lambda name: None)             # no certbot
    monkeypatch.setattr(ssl, "_cert_exists", lambda domain: True)
    monkeypatch.setattr(ssl, "_cert_not_after", lambda domain: "2099-01-01T00:00:00Z")


def test_ssl_is_not_marked_on_when_the_panel_did_not_enable_https(aapanel_site, monkeypatch):
    # re-enable after a disable: the cert is still on disk, nothing new was issued,
    # and the panel refuses to flip the site
    monkeypatch.setattr(panel_api, "http_enable_site_ssl", lambda d: False)
    res = ssl.enable("site", "app.example.com", 8085)
    assert res["ssl"] is False and "did not enable HTTPS" in res["error"]
    assert ssl.read_ssl("site") is False


def test_https_the_operator_enabled_in_the_panel_counts(aapanel_site, monkeypatch, tmp_path):
    # no API key, so the plugin cannot flip the site, but the site already serves TLS
    monkeypatch.setattr(panel_api, "http_enable_site_ssl", lambda d: False)
    conf = tmp_path / "app.example.com.conf"
    monkeypatch.setattr(panel_api, "_AAPANEL_SITE_CONF", str(tmp_path / "%s.conf"))
    conf.write_text("server {\n  listen 80;\n  #listen 443 ssl;\n  #ssl_certificate /x;\n}\n")
    assert ssl.enable("site", "app.example.com", 8085)["ssl"] is False      # commented out is not on
    conf.write_text("server {\n  listen 80;\n  listen 443 ssl http2;\n"
                    "  ssl_certificate /www/server/panel/vhost/cert/app.example.com/fullchain.pem;\n}\n")
    res = ssl.enable("site", "app.example.com", 8085)
    assert res["ssl"] is True and ssl.read_ssl("site") is True


def test_ssl_is_marked_on_once_the_panel_confirms(aapanel_site, monkeypatch):
    monkeypatch.setattr(panel_api, "http_enable_site_ssl", lambda d: True)
    res = ssl.enable("site", "app.example.com", 8085)
    assert res["ssl"] is True and res["via"] == "existing-cert"
    assert ssl.read_ssl("site") is True


def test_an_expired_leftover_cert_is_not_reported_as_ssl(aapanel_site, monkeypatch):
    monkeypatch.setattr(ssl, "_cert_not_after", lambda domain: "2020-01-01T00:00:00Z")
    monkeypatch.setattr(panel_api, "http_enable_site_ssl", lambda d: True)
    res = ssl.enable("site", "app.example.com", 8085)
    assert res["ssl"] is False and "expired on 2020-01-01" in res["error"]
    assert ssl.read_ssl("site") is False


# ---- nginx reload ------------------------------------------------------------ #
def test_failed_nginx_reload_is_reported_and_rolled_back(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_api, "detect_webserver", lambda: "nginx")
    domain = "jhprobe.example.com"
    vhost = tmp_path / "vhost" / "nginx"
    site_conf = vhost / ("%s.conf" % domain)
    site_conf.parent.mkdir(parents=True)
    original = ("server\n{\n    listen 80;\n    server_name %s;\n"
                "    #REWRITE-START\n    include /tmp/rw.conf;\n    #REWRITE-END\n"
                "    #error_page 404 /404.html;\n}\n" % domain)
    site_conf.write_text(original)
    proxyfile = tmp_path / "data" / "proxyfile.json"
    proxyfile.parent.mkdir(parents=True)
    proxyfile.write_text("[]")
    monkeypatch.setattr(panel_api, "_AAPANEL_SITE_CONF", str(vhost / "%s.conf"))
    monkeypatch.setattr(panel_api, "_AAPANEL_PROXY_DIR", str(vhost / "proxy" / "%s"))
    monkeypatch.setattr(panel_api, "_AAPANEL_PROXYFILE", str(proxyfile))

    reload_err = ["nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)"]

    def run(argv, check=False, timeout=15):
        if "reload" in argv:
            return (1, "", reload_err[0])
        return (0, "syntax is ok", "")
    monkeypatch.setattr(panel_api, "run", run)

    ok, err = panel_api.write_aapanel_proxy_files(domain, 18080)
    assert ok is False and "reload failed" in err and "bind()" in err
    assert site_conf.read_text() == original and proxyfile.read_text() == "[]"

    # nginx simply not running is different: the config is valid and applies when
    # it starts, and rolling back would orphan the site that was just created
    reload_err[0] = 'nginx: [error] open() "/www/server/nginx/logs/nginx.pid" failed (2: No such file or directory)'
    ok, err = panel_api.write_aapanel_proxy_files(domain, 18080)
    assert ok is True and site_conf.read_text() != original


# ---- wipe -------------------------------------------------------------------- #
@pytest.fixture
def wipe_env(tmp_path, monkeypatch):
    data = tmp_path / "javahost"
    (data / "backups").mkdir(parents=True)
    (data / "backups" / "backup-a.tar.gz").write_text("only copy")
    fs_roots = tuple(fs.MANAGED_ROOTS) + (str(tmp_path),)
    monkeypatch.setattr(fs, "MANAGED_ROOTS", fs_roots)
    monkeypatch.setattr(maintenance, "DATA_ROOT", str(data))
    monkeypatch.setattr(maintenance, "UNINSTALL_PLAN", str(data / ".uninstall_plan"))
    clean = {"removed": [], "errors": {}}
    for step in ("_wipe_apps", "_wipe_sites", "_wipe_tomcats", "_wipe_jdks"):
        monkeypatch.setattr(maintenance, step, lambda: dict(clean))
    return data


def test_full_wipe_keeps_the_data_root_when_a_step_failed(wipe_env, monkeypatch):
    monkeypatch.setattr(maintenance, "_wipe_apps", lambda: {
        "removed": [], "errors": {"shop": "aaPanel site delete did not confirm removal"}})
    res = maintenance.wipe("full", "WIPE")
    assert res["ok"] is False and res["performed"] is True
    assert res["steps"]["data_root"]["removed"] is False
    assert "apps did not finish" in res["steps"]["data_root"]["errors"]["data_root"]
    assert (wipe_env / "backups" / "backup-a.tar.gz").read_text() == "only copy"


def test_full_wipe_keeps_the_data_root_when_a_runtime_was_skipped(wipe_env, monkeypatch):
    monkeypatch.setattr(maintenance, "_wipe_jdks", lambda: {
        "removed": [], "errors": {}, "skipped": {"jdk-17": ["shop"]}})
    res = maintenance.wipe("full", "WIPE")
    assert wipe_env.is_dir()
    assert res["ok"] is False and "jdks did not finish" in res["steps"]["data_root"]["errors"]["data_root"]


def test_forced_full_wipe_finishes_and_still_reports_what_failed(wipe_env, monkeypatch):
    # plugin uninstall: there is no second attempt, the panel may be unreachable
    seen = []
    monkeypatch.setattr(maintenance, "_wipe_apps", lambda force=False: seen.append(force) or {
        "removed": ["shop"], "errors": {}})
    monkeypatch.setattr(maintenance, "_wipe_sites", lambda: {
        "removed": [], "errors": {"shop": "panel unreachable"}})
    res = maintenance.wipe("full", "WIPE", force=True)
    assert seen == [True] and not wipe_env.exists()
    assert res["ok"] is False and res["steps"]["sites"]["errors"] == {"shop": "panel unreachable"}


def test_forced_wipe_removes_an_app_whose_site_cannot_be_confirmed(blocked_app):
    base, touched = blocked_app
    monkeypatch_apps = maintenance._list_apps
    maintenance._list_apps = lambda: ["demo"]
    try:
        assert maintenance._wipe_apps()["errors"]                  # left whole without force
        assert base.is_dir()
        res = maintenance._wipe_apps(force=True)
    finally:
        maintenance._list_apps = monkeypatch_apps
    assert res["removed"] == ["demo"] and not base.exists()


def test_full_wipe_removes_the_data_root_when_everything_finished(wipe_env):
    res = maintenance.wipe("full", "WIPE")
    assert res["ok"] is True and res["steps"]["data_root"]["removed"] is True
    assert not wipe_env.exists()


def test_partial_wipe_reports_not_ok_when_a_step_has_errors(wipe_env, monkeypatch):
    monkeypatch.setattr(maintenance, "_wipe_tomcats", lambda: {
        "removed": [], "errors": {"11": "in use"}, "skipped": {}})
    assert maintenance.wipe("tomcats", "WIPE")["ok"] is False
    assert maintenance.wipe("jdks", "WIPE")["ok"] is True
    # a runtime skipped because an app still uses it is the documented outcome
    monkeypatch.setattr(maintenance, "_wipe_jdks", lambda: {
        "removed": [], "errors": {}, "skipped": {"jdk-17": ["shop"]}})
    res = maintenance.wipe("jdks", "WIPE")
    assert res["ok"] is True and res["steps"]["jdks"]["skipped"] == {"jdk-17": ["shop"]}


def test_wipe_apps_does_not_count_an_app_that_is_still_there(monkeypatch):
    monkeypatch.setattr(maintenance, "_list_apps", lambda: ["gone", "stuck"])
    monkeypatch.setattr(maintenance.service, "action", lambda *a, **k: None)
    monkeypatch.setattr(maintenance.service, "remove_unit", lambda *a, **k: None)
    monkeypatch.setattr(maintenance.instance, "delete", lambda app: (
        {"app": app, "removed": True} if app == "gone" else
        {"app": app, "removed": False, "site_warning": "site still registered"}))
    res = maintenance._wipe_apps()
    assert res["removed"] == ["gone"] and res["errors"] == {"stuck": "site still registered"}


def test_sites_scope_covers_sites_registered_in_aapanel(tmp_path, monkeypatch):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path / "instances"))
    monkeypatch.setattr(maintenance.proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    (tmp_path / "vhost").mkdir()
    (tmp_path / "vhost" / "javahost_legacy.conf").write_text("# Managed by JavaHost\nserver {}\n")
    for app in ("legacy", "shop", "nosite"):
        (tmp_path / "instances" / app / "bin").mkdir(parents=True)
    proxy._store_domain("shop", "shop.example.com")      # aaPanel-owned: no vhost file
    assert maintenance._list_sites() == ["javahost_legacy.conf", "shop.example.com (app shop)"]

    monkeypatch.setattr(maintenance.proxy, "remove_site", lambda app: (
        {"app": app, "removed": True} if app == "legacy" else
        {"app": app, "removed": False, "error": "panel unreachable"}))
    monkeypatch.setattr(maintenance, "_remove_include", lambda *a, **k: False)
    monkeypatch.setattr(maintenance.proxy, "reload_nginx", lambda *a, **k: True)
    res = maintenance._wipe_sites()
    assert res["removed"] == ["legacy"] and res["errors"] == {"shop": "panel unreachable"}


# ---- database env ------------------------------------------------------------ #
@pytest.mark.parametrize("host", ["db.exa mple.com;evil", "db.example.com:3307", "::1",
                                  "[2001:db8::1]", "", "-x.example.com", "a/b"])
def test_db_host_is_rejected_not_rewritten(host):
    with pytest.raises(ValueError, match="invalid database host"):
        dbbase.safe_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "db-1.internal.example.com", "pg_primary"])
def test_db_host_accepts_hostnames_and_ipv4(host):
    assert dbbase.safe_host(host) == host


def test_missing_db_user_is_empty_not_the_string_none():
    env = dbengines.get("postgresql").render_env(host="127.0.0.1", db="shop", user=None, password=None)
    assert env["DB_USER"] == "" and env["DB_PASSWORD"] == ""


def test_db_password_with_a_line_break_is_refused():
    with pytest.raises(ValueError, match="line breaks"):
        dbengines.get("postgresql").render_env(host="127.0.0.1", db="shop", user="u", password="a\nb")


# ---- update check ------------------------------------------------------------ #
def test_a_failed_update_check_is_not_cached_for_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(java, "plugin_majors", lambda: [])
    monkeypatch.setattr(registry, "LINES", {"11": object()})
    monkeypatch.setattr(installer, "is_installed", lambda m: "11.0.22")

    def offline(m, **k):
        raise OSError("network unreachable")
    monkeypatch.setattr(registry, "resolve_latest_patch", offline)
    first = updates.check(force=True, now=1000.0)
    assert first["errors"] and first["tomcat"]["11"]["update"] is False

    monkeypatch.setattr(registry, "resolve_latest_patch", lambda m, **k: "11.0.24")
    assert updates.check(now=1000.0 + 60)["cached"] is True              # brief back-off
    later = updates.check(now=1000.0 + updates.ERROR_TTL_SECONDS + 1)    # then it re-checks
    assert later["cached"] is False and later["tomcat"]["11"]["update"] is True
    assert updates.check(now=1000.0 + 3600 * 5)["cached"] is True        # a good result keeps the day


# ---- UI: the way out of a blocked delete ------------------------------------- #
def test_ui_offers_to_delete_anyway_when_the_site_blocks_it():
    from test_ui_reliability import run_node, section
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "plugin", "javahost", "index.html"), encoding="utf-8").read()
    js = section(html, "function deleteApp(app, btn, force){", "// EVENT WIRING")
    harness = """
    let calls=[], confirms=[true,true], toasts=[];
    function confirm(m){ return confirms.shift(); }
    function appPending(){return false;} function acquireAppPending(){return {};} function releaseAppPending(){}
    function busy(){} function syncAppPendingControls(){} function resultIsUncertain(){return false;}
    function toast(m,k,t){ toasts.push(k); } function refresh(){} function closeDrawer(){}
    function payload(r){return r.msg;} function errText(p){return String(p);}
    function failed(r){return r.status===false;}
    function call(name, data, cb){
      calls.push(data);
      cb(data.force ? {status:true,msg:{app:'shop',removed:true}}
                    : {status:false,msg:'shop was not deleted: aaPanel site delete did not confirm removal'});
    }
    var DRAWER={app:null}; function $(){ return {classList:{contains:function(){return false;}}}; }
    %s
    deleteApp('shop', {});
    console.log(JSON.stringify({calls:calls, toasts:toasts}));
    """ % js
    out = run_node(harness)
    assert out["calls"] == [{"app": "shop"}, {"app": "shop", "force": "1"}]
    assert out["toasts"] == ["ok"]
