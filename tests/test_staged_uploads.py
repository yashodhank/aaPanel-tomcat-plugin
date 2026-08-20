# coding: utf-8
"""Security-contract tests for files staged by aaPanel's upload endpoint."""
import os
from types import SimpleNamespace

import pytest

import javahost_main


def _request(**values):
    return SimpleNamespace(**values)


def test_staged_upload_accepts_a_canonical_regular_file(tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    artifact = staged / "app.war"
    artifact.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))

    with javahost_main._claimed_staged_upload(str(artifact), ".war") as claimed:
        assert claimed != str(artifact)
        assert open(claimed, "rb").read() == b"PK\x03\x04"
        assert not artifact.exists()
    assert not os.path.exists(claimed)


@pytest.mark.parametrize("case", [
    "outside",
    "traversal",
    "symlink",
    "symlinked_parent",
    "directory",
    "wrong_extension",
    "missing",
    "relative",
])
def test_staged_upload_rejects_untrusted_paths(case, tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    valid = staged / "valid.war"
    valid.write_bytes(b"PK\x03\x04")
    outside = tmp_path / "outside.war"
    outside.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))

    if case == "outside":
        candidate = outside
    elif case == "traversal":
        candidate = staged / "unused" / ".." / "valid.war"
    elif case == "symlink":
        candidate = staged / "alias.war"
        candidate.symlink_to(valid)
    elif case == "symlinked_parent":
        real_dir = staged / "real"
        real_dir.mkdir()
        (real_dir / "nested.war").write_bytes(b"PK\x03\x04")
        alias_dir = staged / "alias"
        alias_dir.symlink_to(real_dir, target_is_directory=True)
        candidate = alias_dir / "nested.war"
    elif case == "directory":
        candidate = staged / "directory.war"
        candidate.mkdir()
    elif case == "wrong_extension":
        candidate = staged / "app.jar"
        candidate.write_bytes(b"PK\x03\x04")
    elif case == "missing":
        candidate = staged / "missing.war"
    else:
        candidate = "valid.war"

    with pytest.raises(ValueError, match="valid staged upload"):
        with javahost_main._claimed_staged_upload(str(candidate), ".war"):
            pass


def test_create_jar_app_passes_only_validated_staged_path(tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    artifact = staged / "app.jar"
    artifact.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))
    received = {}

    def fake_create_jar(**kwargs):
        received.update(kwargs)
        received["snapshot"] = open(kwargs["jar_src"], "rb").read()
        return {"app": "demo", "port": 8080, "springboot": True}

    monkeypatch.setattr(javahost_main.instance, "create_jar", fake_create_jar)
    result = javahost_main.javahost_main().CreateJarApp(
        _request(app="demo", jar=str(artifact), java=17, port=8080,
                 memory=512, profiles=""))

    assert result["status"] is True
    assert os.path.basename(received["jar_src"]) == "app.jar"
    assert ".javahost-claim-" in received["jar_src"]
    assert received["snapshot"] == b"PK\x03\x04"
    assert not os.path.exists(received["jar_src"])


def test_upload_war_passes_only_validated_staged_path(tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    artifact = staged / "app.war"
    artifact.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))
    deployed = []

    monkeypatch.setattr(javahost_main.instance, "require_tomcat_war_target",
                        lambda app: "/managed/ROOT")
    monkeypatch.setattr(javahost_main.registry, "get_line",
                        lambda major: SimpleNamespace(namespace="jakarta"))
    monkeypatch.setattr(javahost_main.war, "namespace_warning", lambda *args: None)
    def fake_replace_root(src, target):
        deployed.append((src, target, open(src, "rb").read()))

    monkeypatch.setattr(javahost_main.war, "replace_root", fake_replace_root)
    monkeypatch.setattr(javahost_main.service, "action", lambda *args: None)
    monkeypatch.setattr(javahost_main.service, "status", lambda app: "active")

    result = javahost_main.javahost_main().UploadWar(
        _request(app="demo", tmp=str(artifact), war=None, version="10"))

    assert result["status"] is True
    assert os.path.basename(deployed[0][0]) == "app.war"
    assert ".javahost-claim-" in deployed[0][0]
    assert deployed[0][1:] == ("/managed/ROOT", b"PK\x03\x04")
    assert not os.path.exists(deployed[0][0])


def test_migrate_war_passes_only_validated_staged_path(tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    artifact = staged / "legacy.war"
    artifact.write_bytes(b"PK\x03\x04")
    work = tmp_path / "migration-work"
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))
    migrated = []

    monkeypatch.setattr(javahost_main.instance, "require_tomcat_war_target",
                        lambda app: "/managed/ROOT")
    monkeypatch.setattr(javahost_main.installer, "ensure_java", lambda major: "/java")
    monkeypatch.setattr(javahost_main.fs, "mkdtemp",
                        lambda prefix: (work.mkdir(), str(work))[1])
    def fake_migrate(src, out, java_home):
        migrated.append((src, out, java_home, open(src, "rb").read()))

    monkeypatch.setattr(javahost_main.war, "migrate", fake_migrate)
    monkeypatch.setattr(javahost_main.war, "replace_root", lambda *args: None)
    monkeypatch.setattr(javahost_main.service, "action", lambda *args: None)
    monkeypatch.setattr(javahost_main.service, "status", lambda app: "active")

    result = javahost_main.javahost_main().MigrateWar(
        _request(app="demo", war=str(artifact), tmp=None, version="10"))

    assert result["status"] is True
    assert os.path.basename(migrated[0][0]) == "legacy.war"
    assert ".javahost-claim-" in migrated[0][0]
    assert migrated[0][3] == b"PK\x03\x04"
    assert not os.path.exists(migrated[0][0])


@pytest.mark.parametrize(("method", "request_obj", "blocked_dependency"), [
    ("CreateJarApp",
     _request(app="demo", jar=None, tmp=None, java=17, port=8080,
              memory=512, profiles=""),
     ("instance", "create_jar")),
    ("UploadWar",
     _request(app="demo", tmp=None, war=None, version="10"),
     ("instance", "require_tomcat_war_target")),
    ("MigrateWar",
     _request(app="demo", tmp=None, war=None, version="10"),
     ("instance", "require_tomcat_war_target")),
])
def test_deployment_endpoints_reject_before_downstream_work(
        method, request_obj, blocked_dependency, tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    outside = tmp_path / ("outside.jar" if method == "CreateJarApp" else "outside.war")
    outside.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))
    if method == "CreateJarApp":
        request_obj.jar = str(outside)
    else:
        request_obj.war = str(outside)

    owner = getattr(javahost_main, blocked_dependency[0])

    def must_not_run(*args, **kwargs):
        raise AssertionError("downstream deployment ran for an invalid upload")

    monkeypatch.setattr(owner, blocked_dependency[1], must_not_run)
    result = getattr(javahost_main.javahost_main(), method)(request_obj)

    assert result["status"] is False
    assert "valid staged upload" in result["msg"]


@pytest.mark.parametrize(("method", "extension", "blocked_dependency"), [
    ("CreateJarApp", ".jar", ("instance", "create_jar")),
    ("UploadWar", ".war", ("instance", "require_tomcat_war_target")),
    ("MigrateWar", ".war", ("instance", "require_tomcat_war_target")),
])
def test_deployment_endpoints_reject_a_swap_during_atomic_claim(
        method, extension, blocked_dependency, tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    artifact = staged / ("app" + extension)
    artifact.write_bytes(b"trusted")
    outside = tmp_path / ("outside" + extension)
    outside.write_bytes(b"untrusted")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))

    request_obj = _request(
        app="demo", jar=str(artifact) if extension == ".jar" else None,
        war=str(artifact) if extension == ".war" else None, tmp=None,
        java=17, port=8080, memory=512, profiles="", version="10")
    owner = getattr(javahost_main, blocked_dependency[0])
    downstream_ran = []
    monkeypatch.setattr(owner, blocked_dependency[1],
                        lambda *args, **kwargs: downstream_ran.append((args, kwargs)))

    real_rename = javahost_main.os.rename

    def swap_then_rename(source, destination):
        if source == str(artifact):
            artifact.unlink()
            artifact.symlink_to(outside)
        return real_rename(source, destination)

    monkeypatch.setattr(javahost_main.os, "rename", swap_then_rename)
    result = getattr(javahost_main.javahost_main(), method)(request_obj)

    assert result["status"] is False
    assert "valid staged upload" in result["msg"]
    assert downstream_ran == []


def test_storage_profile_label_precedes_legacy_name():
    fields = javahost_main.javahost_main()._profile_fields(
        _request(label="Router-safe label", name="legacy direct-client name"))

    assert fields["name"] == "Router-safe label"


def test_storage_profile_empty_label_still_precedes_legacy_name():
    fields = javahost_main.javahost_main()._profile_fields(
        _request(label="", name="legacy direct-client name"))

    assert fields["name"] == ""


def test_storage_profile_uses_legacy_name_only_when_label_is_absent():
    fields = javahost_main.javahost_main()._profile_fields(
        _request(name="legacy direct-client name"))

    assert fields["name"] == "legacy direct-client name"
