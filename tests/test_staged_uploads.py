# coding: utf-8
"""Security-contract tests for files staged by aaPanel's upload endpoint."""
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

    assert javahost_main._staged_upload_path(str(artifact), ".war") == str(artifact)


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
        javahost_main._staged_upload_path(str(candidate), ".war")


def test_create_jar_app_passes_only_validated_staged_path(tmp_path, monkeypatch):
    staged = tmp_path / "panel-uploads"
    staged.mkdir()
    artifact = staged / "app.jar"
    artifact.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(javahost_main, "_AAPANEL_STAGED_UPLOAD_ROOT", str(staged))
    received = {}

    def fake_create_jar(**kwargs):
        received.update(kwargs)
        return {"app": "demo", "port": 8080, "springboot": True}

    monkeypatch.setattr(javahost_main.instance, "create_jar", fake_create_jar)
    result = javahost_main.javahost_main().CreateJarApp(
        _request(app="demo", jar=str(artifact), java=17, port=8080,
                 memory=512, profiles=""))

    assert result["status"] is True
    assert received["jar_src"] == str(artifact)


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
    monkeypatch.setattr(javahost_main.war, "replace_root",
                        lambda src, target: deployed.append((src, target)))
    monkeypatch.setattr(javahost_main.service, "action", lambda *args: None)
    monkeypatch.setattr(javahost_main.service, "status", lambda app: "active")

    result = javahost_main.javahost_main().UploadWar(
        _request(app="demo", tmp=str(artifact), war=None, version="10"))

    assert result["status"] is True
    assert deployed == [(str(artifact), "/managed/ROOT")]


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
    monkeypatch.setattr(javahost_main.war, "migrate",
                        lambda src, out, java_home: migrated.append((src, out, java_home)))
    monkeypatch.setattr(javahost_main.war, "replace_root", lambda *args: None)
    monkeypatch.setattr(javahost_main.service, "action", lambda *args: None)
    monkeypatch.setattr(javahost_main.service, "status", lambda app: "active")

    result = javahost_main.javahost_main().MigrateWar(
        _request(app="demo", war=str(artifact), tmp=None, version="10"))

    assert result["status"] is True
    assert migrated[0][0] == str(artifact)


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
