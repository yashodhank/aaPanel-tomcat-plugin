# coding: utf-8
"""Offline tests for upstream-update detection (no network). java/registry probes
are monkeypatched; the cache file is redirected into tmp."""
from core import updates
from core.runtime import java
from core.tomcat import registry, installer


def test_version_newer_semver():
    assert java.version_newer("17.0.11+9", "17.0.10+7") is True
    assert java.version_newer("17.0.10+7", "17.0.10+7") is False
    assert java.version_newer("17.0.9+1", "17.0.10+1") is False
    assert java.version_newer(None, "17.0.1") is False
    assert java.version_newer("17.0.1", None) is False


def test_installed_jdk_version_reads_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(java, "_PLUGIN_RUNTIMES", str(tmp_path))
    d = tmp_path / "jdk-17"
    d.mkdir()
    (d / ".javahost-jdk-version").write_text("17.0.10+7\n")
    assert java.installed_jdk_version(17) == "17.0.10+7"
    assert java.installed_jdk_version(21) is None  # no marker


def _patch_common(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(java, "plugin_majors", lambda: [17])
    monkeypatch.setattr(java, "installed_jdk_version", lambda m: "17.0.10+7")
    monkeypatch.setattr(java, "resolve_latest_jdk", lambda m: "17.0.11+9")
    monkeypatch.setattr(registry, "LINES", {"11": object()})
    monkeypatch.setattr(installer, "is_installed", lambda m: "11.0.22")
    monkeypatch.setattr(registry, "resolve_latest_patch", lambda m, **k: "11.0.24")


def test_check_flags_available_updates(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    res = updates.check(force=True, now=1000.0)
    assert res["java"]["17"]["update"] is True
    assert res["java"]["17"]["latest"] == "17.0.11+9"
    assert res["tomcat"]["11"]["update"] is True
    assert res["cached"] is False


def test_check_no_update_when_current(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(java, "resolve_latest_jdk", lambda m: "17.0.10+7")
    monkeypatch.setattr(registry, "resolve_latest_patch", lambda m, **k: "11.0.22")
    res = updates.check(force=True, now=1000.0)
    assert res["java"]["17"]["update"] is False
    assert res["tomcat"]["11"]["update"] is False


def test_cache_hit_and_force_bypass(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    first = updates.check(force=True, now=1000.0)
    assert first["cached"] is False
    # within TTL → served from cache (don't recompute)
    cached = updates.check(force=False, now=1000.0 + 100)
    assert cached["cached"] is True
    # force bypasses the cache
    fresh = updates.check(force=True, now=1000.0 + 100)
    assert fresh["cached"] is False


def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    updates.check(force=True, now=1000.0)
    later = updates.check(force=False, now=1000.0 + updates.TTL_SECONDS + 1)
    assert later["cached"] is False


def test_network_error_surfaces_not_crashes(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    def boom(m):
        raise RuntimeError("offline")
    monkeypatch.setattr(java, "resolve_latest_jdk", boom)
    res = updates.check(force=True, now=1000.0)
    assert "java-17" in res["errors"]
    assert res["java"]["17"]["latest"] is None
    assert res["java"]["17"]["update"] is False


def test_tomcat_network_error_surfaced_not_false_uptodate(tmp_path, monkeypatch):
    """A Tomcat index fetch failure must record an error + latest=None + update=False
    — never a confident 'up to date' (update=False with a real latest)."""
    _patch_common(monkeypatch, tmp_path)
    def boom(m, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(registry, "resolve_latest_patch", boom)
    res = updates.check(force=True, now=1000.0)
    assert "tomcat-11" in res["errors"]
    assert res["tomcat"]["11"]["latest"] is None
    assert res["tomcat"]["11"]["update"] is False


def test_invalidate_removes_cache(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    updates.check(force=True, now=1000.0)
    updates.invalidate()
    # next non-force check must recompute (cache gone), not read stale
    res = updates.check(force=False, now=1000.0 + 1)
    assert res["cached"] is False
