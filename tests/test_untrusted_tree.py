# coding: utf-8
"""The instance tree (<instances>/<app>/...) is chowned to the app's own user,
so root-run plugin code must not trust what it finds there. Covers the domain
marker, the decision to delete an aaPanel site, and the conf/ permission pass."""
import os
import sys
import types

import pytest

from core.compat import aapanel as panel_api
from core.deploy import proxy
from core.tomcat import hardening, instance


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path / "instances"))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    bin_dir = tmp_path / "instances" / "demo" / "bin"
    bin_dir.mkdir(parents=True)
    return bin_dir


# ---- domain marker ---------------------------------------------------------- #
@pytest.mark.parametrize("planted", [
    "../../../etc", "victim.com/../../x", "a b.com", "evil.com\nother.com",
    "http://169.254.169.254/", "-leading.com", "x" * 300, "", "\n",
])
def test_read_domain_rejects_anything_that_is_not_a_hostname(app, planted):
    (app / "site.domain").write_text(planted)
    assert proxy.read_domain("demo") is None


def test_read_domain_returns_a_stored_domain(app):
    proxy._store_domain("demo", "demo.example.com")
    assert proxy.read_domain("demo") == "demo.example.com"
    (app / "site.domain").write_text("  Demo.Example.COM \n")
    assert proxy.read_domain("demo") == "demo.example.com"


# ---- who may delete an aaPanel site ----------------------------------------- #
def _rows(monkeypatch, rows):
    """Panel answers a sites lookup with `rows` (None = lookup failed)."""
    monkeypatch.setattr(panel_api.config, "aapanel_api_key", lambda: "k")
    monkeypatch.setattr(panel_api, "_http_site_rows", lambda d: rows)


OURS = {"id": 7, "name": "demo.example.com",
        "ps": "JavaHost: demo.example.com -> http://127.0.0.1:8080"}


@pytest.mark.parametrize("rows,expected", [
    ([OURS], "javahost"),
    ([{"id": 7, "name": "demo.example.com", "ps": "my wordpress"}], "foreign"),
    ([{"id": 7, "name": "demo.example.com", "ps": ""}], "foreign"),
    # a marker for another domain, or with trailing junk, is not ours
    ([{"id": 7, "name": "demo.example.com",
       "ps": "JavaHost: other.example.com -> http://127.0.0.1:8080"}], "foreign"),
    ([{"id": 7, "name": "demo.example.com", "ps": OURS["ps"] + " (edited)"}], "foreign"),
    ([{"id": 9, "name": "notdemo.example.com", "ps": OURS["ps"]}], "absent"),   # search is fuzzy
    ([], "absent"),
    (None, "unknown"),
])
def test_site_ownership(monkeypatch, rows, expected):
    _rows(monkeypatch, rows)
    monkeypatch.setattr(panel_api, "public", None)
    assert panel_api.site_ownership("demo.example.com") == expected


def test_site_ownership_falls_back_to_the_panel_db_without_an_api_key(monkeypatch):
    monkeypatch.setattr(panel_api.config, "aapanel_api_key", lambda: "")

    class _M(object):
        def where(self, *a): return self
        def field(self, *a): return self
        def select(self): return [OURS]
    monkeypatch.setattr(panel_api, "public", types.SimpleNamespace(M=lambda table: _M()))
    assert panel_api.site_ownership("demo.example.com") == "javahost"


def _forbid_delete(monkeypatch):
    """Every way of deleting a site raises if reached."""
    def boom(*a, **k):
        raise AssertionError("a site that is not JavaHost's must never be deleted")
    monkeypatch.setattr(panel_api, "http_request", boom)
    fake = types.ModuleType("panelSite")
    fake.panelSite = type("panelSite", (), {"DeleteSite": boom})
    monkeypatch.setitem(sys.modules, "panelSite", fake)


@pytest.mark.parametrize("state,result", [("foreign", False), ("unknown", False), ("absent", True)])
def test_aapanel_remove_site_only_deletes_javahost_sites(monkeypatch, state, result):
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: state)
    _forbid_delete(monkeypatch)
    assert proxy.aapanel_remove_site("victim.example.com") is result


def test_http_remove_site_rechecks_the_marker(monkeypatch):
    _rows(monkeypatch, [{"id": 7, "name": "victim.example.com", "ps": "my wordpress"}])
    _forbid_delete(monkeypatch)
    assert panel_api.http_remove_site("victim.example.com") is False


def test_planted_marker_cannot_delete_someone_elses_site(app, monkeypatch):
    # code running as the app user points the marker at a site it does not own
    (app / "site.domain").write_text("victim.example.com\n")
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: "foreign")
    _forbid_delete(monkeypatch)
    res = proxy.remove_site("demo")
    # the app's own cleanup still completes, and says what it left alone
    assert res["removed"] is True and res["aapanel_cleaned"] is False
    assert "victim.example.com" in res["note"]
    assert proxy.read_domain("demo") is None


def test_delete_app_is_not_blocked_by_a_site_it_does_not_own(app, monkeypatch, tmp_path):
    from core.util import fs
    monkeypatch.setattr(fs, "MANAGED_ROOTS", tuple(fs.MANAGED_ROOTS) + (str(tmp_path),))
    fs.mark_managed(str(app.parent))
    (app / "site.domain").write_text("operator-site.example.com\n")
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: "foreign")
    monkeypatch.setattr(instance.service, "remove_unit", lambda a: None)
    _forbid_delete(monkeypatch)
    out = instance.delete("demo")
    assert out["removed"] is True and not app.parent.exists()


def test_site_deleted_by_hand_does_not_block_removal(app, monkeypatch):
    proxy._store_domain("demo", "demo.example.com")
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: "absent")
    _forbid_delete(monkeypatch)
    res = proxy.remove_site("demo")
    assert res["removed"] is True and proxy.read_domain("demo") is None


def test_unreachable_panel_keeps_the_marker_for_retry(app, monkeypatch):
    proxy._store_domain("demo", "demo.example.com")
    monkeypatch.setattr(panel_api, "site_ownership", lambda d: "unknown")
    _forbid_delete(monkeypatch)
    res = proxy.remove_site("demo")
    assert res["removed"] is False and proxy.read_domain("demo") == "demo.example.com"


# ---- conf/ permission pass -------------------------------------------------- #
def test_secure_perms_does_not_chmod_through_links(tmp_path):
    base = tmp_path / "base"
    (base / "conf" / "sub").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("x")
    os.chmod(str(victim), 0o644)
    os.symlink(str(victim), str(base / "conf" / "planted.xml"))
    real = base / "conf" / "server.xml"
    real.write_text("<Server/>")
    os.chmod(str(real), 0o666)
    users = base / "conf" / "sub" / "tomcat-users.xml"
    users.write_text("<tomcat-users/>")
    os.chmod(str(users), 0o666)

    hardening.secure_perms(str(base))
    assert os.stat(str(victim)).st_mode & 0o777 == 0o644      # untouched
    assert os.stat(str(real)).st_mode & 0o777 == 0o640
    assert os.stat(str(users)).st_mode & 0o777 == 0o600


def test_secure_perms_skips_a_symlinked_conf_dir(tmp_path):
    elsewhere = tmp_path / "etc"
    elsewhere.mkdir()
    victim = elsewhere / "shadow"
    victim.write_text("x")
    os.chmod(str(victim), 0o600)
    base = tmp_path / "base"
    base.mkdir()
    os.symlink(str(elsewhere), str(base / "conf"))
    hardening.secure_perms(str(base))
    assert os.stat(str(victim)).st_mode & 0o777 == 0o600


# ---- review follow-ups ------------------------------------------------------ #
def test_tail_log_does_not_read_through_links(app, tmp_path):
    secret = tmp_path / "shadow"
    secret.write_text("root:$6$secret\n")
    logs = app.parent / "logs"
    logs.mkdir()
    os.symlink(str(secret), str(logs / "catalina.out"))
    assert "secret" not in instance.tail_log("demo", 10)
    (logs / "catalina.2026.log").write_text("real line\n")
    assert instance.tail_log("demo", 10) == "real line"


def test_tail_log_skips_a_symlinked_logs_dir(app, tmp_path):
    elsewhere = tmp_path / "var-log"
    elsewhere.mkdir()
    (elsewhere / "catalina.out").write_text("not this app's log\n")
    os.symlink(str(elsewhere), str(app.parent / "logs"))
    assert instance.tail_log("demo", 10) == ""


def test_marker_is_not_written_through_a_symlinked_bin(app, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    app.rmdir()
    os.symlink(str(victim), str(app))
    with pytest.raises(RuntimeError, match="symlinked"):
        proxy._store_domain("demo", "demo.example.com")
    with pytest.raises(RuntimeError, match="symlinked"):
        proxy._store_owner("demo", "aapanel")
    assert os.listdir(str(victim)) == [] and os.stat(str(victim)).st_mode & 0o777 == 0o700


def test_secure_perms_tolerates_a_missing_conf_and_skips_hardlinks(tmp_path):
    hardening.secure_perms(str(tmp_path / "nothing-here"))
    base = tmp_path / "base"
    (base / "conf").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("x")
    os.chmod(str(victim), 0o644)
    os.link(str(victim), str(base / "conf" / "linked.xml"))
    hardening.secure_perms(str(base))
    assert os.stat(str(victim)).st_mode & 0o777 == 0o644
