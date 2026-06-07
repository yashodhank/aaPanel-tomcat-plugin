# coding: utf-8
"""Offline tests for the multi-profile storage registry (core/backup/remote.py).
No network: only the registry CRUD, migration, and secret-safety are exercised."""
import json
import os
import stat

import pytest

from core.backup import remote


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "REGISTRY_PATH", str(tmp_path / "remotes.json"))
    monkeypatch.setattr(remote, "LEGACY_PATH", str(tmp_path / "remote.json"))
    return tmp_path


def _add(name="Wasabi prod", pid="", bucket="b", secret="SEKRET"):
    return remote.add_profile(name=name, provider="wasabi",
                              endpoint="https://s3.us-east-1.wasabisys.com",
                              region="us-east-1", bucket=bucket, access_key="AK",
                              secret_key=secret, prefix="javahost", pid=pid)


def test_add_list_get_secret_safe(reg):
    r = _add()
    assert r["id"] == "wasabi-prod" and r["secret_set"] is True and "secret_key" not in r
    profs = remote.list_profiles()
    assert len(profs) == 1 and all("secret_key" not in p for p in profs)
    # registry file is 0600 and DOES hold the secret (server-side only)
    assert stat.S_IMODE(os.stat(remote.REGISTRY_PATH).st_mode) == 0o600
    assert "SEKRET" in open(remote.REGISTRY_PATH).read()
    # full (server-side) view exposes it; redacted never does
    assert remote.get_profile("wasabi-prod", redacted=False)["secret_key"] == "SEKRET"
    assert "secret_key" not in remote.get_profile("wasabi-prod", redacted=True)


def test_duplicate_id_rejected(reg):
    _add(pid="dup")
    with pytest.raises(ValueError):
        _add(pid="dup")


def test_update_keeps_secret_on_empty(reg):
    _add(pid="p1", secret="ORIG")
    remote.update_profile("p1", bucket="b2", secret_key="")
    assert remote.get_profile("p1", redacted=False)["secret_key"] == "ORIG"
    assert remote.get_profile("p1")["bucket"] == "b2"
    remote.update_profile("p1", secret_key="NEW")
    assert remote.get_profile("p1", redacted=False)["secret_key"] == "NEW"


def test_enabled_ids_and_configured(reg):
    _add(pid="on")
    _add(pid="off")
    remote.update_profile("off", enabled=False)
    assert remote.enabled_ids() == ["on"]
    assert remote.configured() is True


def test_delete_profile(reg):
    _add(pid="gone")
    assert remote.delete_profile("gone")["removed"] is True
    assert remote.list_profiles() == []
    assert remote.delete_profile("missing")["removed"] is False


def test_legacy_migration(reg):
    # a legacy single-config remote.json -> one "default" profile on first load
    legacy = {"provider": "wasabi", "endpoint": "https://s3.example.com",
              "region": "us-east-1", "bucket": "old", "access_key": "AK",
              "secret_key": "LEGACY", "prefix": "", "path_style": True}
    open(remote.LEGACY_PATH, "w").write(json.dumps(legacy))
    profs = remote.list_profiles()
    assert len(profs) == 1 and profs[0]["id"] == "default" and profs[0]["bucket"] == "old"
    assert os.path.isfile(remote.REGISTRY_PATH)            # registry now persisted
    assert remote.get_profile("default", redacted=False)["secret_key"] == "LEGACY"


def test_resolve_ids(reg):
    _add(pid="a"); _add(pid="b")
    assert set(remote._resolve_ids("all")) == {"a", "b"}
    assert remote._resolve_ids(None) == []
    assert remote._resolve_ids("a,b") == ["a", "b"]
    assert remote._resolve_ids(["a"]) == ["a"]
