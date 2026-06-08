# coding: utf-8
"""First-run wizard endpoints: fresh-state detection, dismissal persistence, and
site-suffix set/validate. config.json is redirected into tmp."""
from types import SimpleNamespace

import javahost_main
from core import config
from core.runtime import java
from core.tomcat import installer, instance


def G(**kw):
    """Mimic aaPanel's `get` namespace (panel.attr uses getattr, not dict keys)."""
    return SimpleNamespace(**kw)


def _api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    return javahost_main.javahost_main()


def _fresh(monkeypatch, fresh=True):
    monkeypatch.setattr(java, "detect", lambda: ({} if fresh else {17: "/jdk17"}))
    monkeypatch.setattr(installer, "is_installed", lambda m: None)
    monkeypatch.setattr(instance, "list_apps", lambda: [])


def test_first_run_fresh_install(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    _fresh(monkeypatch, True)
    msg = api.GetFirstRunState()["msg"]
    assert msg["is_fresh"] is True
    assert msg["needs_suffix"] is True
    assert msg["wizard_done"] is False


def test_first_run_not_fresh_when_jdk_present(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    _fresh(monkeypatch, False)
    assert api.GetFirstRunState()["msg"]["is_fresh"] is False


def test_mark_wizard_done_persists(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    _fresh(monkeypatch, True)
    assert api.GetFirstRunState()["msg"]["wizard_done"] is False
    assert api.MarkWizardDone()["status"] is True
    # re-read: the flag survives (written to config.json) so it won't auto-reopen
    assert api.GetFirstRunState()["msg"]["wizard_done"] is True


def test_set_site_suffix_valid_and_invalid(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    _fresh(monkeypatch, True)
    ok = api.SetSiteSuffix(G(suffix="apps.example.com"))
    assert ok["status"] is True and ok["msg"]["site_suffix"] == "apps.example.com"
    assert config.site_suffix() == "apps.example.com"
    assert api.GetFirstRunState()["msg"]["needs_suffix"] is False
    bad = api.SetSiteSuffix(G(suffix="not a domain!!"))
    assert bad["status"] is False                      # validation rejects it
    # clearing is allowed
    cleared = api.SetSiteSuffix(G(suffix=""))
    assert cleared["status"] is True and cleared["msg"]["site_suffix"] == ""
