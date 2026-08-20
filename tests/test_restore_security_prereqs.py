# coding: utf-8
"""Security regressions for restore inputs and generated service scripts."""
import io
import os
import pwd
import subprocess
import tarfile
import time
from types import SimpleNamespace

import pytest

from core.backup import archive
from core.backup.archive import UnsafeArchive
from core.tomcat import service, templating


def _write_tar(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


def test_extract_rejects_member_count_before_writing_payload(tmp_path, monkeypatch):
    source = tmp_path / "many.tar.gz"
    _write_tar(source, [("one", b"1"), ("two", b"2"), ("three", b"3")])
    monkeypatch.setattr(archive, "MAX_ARCHIVE_MEMBERS", 2)
    destination = tmp_path / "out"

    with pytest.raises(UnsafeArchive, match="too many members"):
        archive.safe_extract_tar(str(source), str(destination))

    assert not list(destination.iterdir())


@pytest.mark.parametrize(
    ("limit_name", "limit", "message"),
    [
        ("MAX_MEMBER_BYTES", 31, "member too large"),
        ("MAX_TOTAL_EXTRACTED_BYTES", 31, "expanded size"),
    ],
)
def test_extract_rejects_expansion_budgets_before_writing(
        tmp_path, monkeypatch, limit_name, limit, message):
    source = tmp_path / "bomb.tar.gz"
    _write_tar(source, [("payload", b"A" * 32)])
    monkeypatch.setattr(archive, limit_name, limit)
    destination = tmp_path / "out"

    with pytest.raises(UnsafeArchive, match=message):
        archive.safe_extract_tar(str(source), str(destination))

    assert not list(destination.iterdir())


def test_extract_rejects_compressed_input_budget(tmp_path, monkeypatch):
    source = tmp_path / "large-input.tar.gz"
    _write_tar(source, [("payload", b"data")])
    monkeypatch.setattr(archive, "MAX_COMPRESSED_ARCHIVE_BYTES", 1)

    with pytest.raises(UnsafeArchive, match="compressed archive too large"):
        archive.safe_extract_tar(str(source), str(tmp_path / "out"))


def test_extract_checks_destination_free_space(tmp_path, monkeypatch):
    source = tmp_path / "needs-space.tar.gz"
    _write_tar(source, [("payload", b"data")])
    monkeypatch.setattr(archive, "MIN_FREE_SPACE_BYTES", 10)
    monkeypatch.setattr(
        archive.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=95, free=5),
    )

    with pytest.raises(UnsafeArchive, match="insufficient free space"):
        archive.safe_extract_tar(str(source), str(tmp_path / "out"))


def test_extract_uses_same_open_file_when_archive_path_is_replaced(tmp_path, monkeypatch):
    source = tmp_path / "source.tar.gz"
    replacement = tmp_path / "replacement.tar.gz"
    _write_tar(source, [("payload", b"trusted")])
    _write_tar(replacement, [("payload", b"swapped")])
    original_preflight = archive._preflight

    def replace_after_preflight(open_file, base_real):
        result = original_preflight(open_file, base_real)
        os.replace(replacement, source)
        return result

    monkeypatch.setattr(archive, "_preflight", replace_after_preflight)
    destination = tmp_path / "out"
    archive.safe_extract_tar(str(source), str(destination))

    assert (destination / "payload").read_bytes() == b"trusted"


def test_manifest_reader_refuses_oversized_member(tmp_path, monkeypatch):
    source = tmp_path / "manifest.tar.gz"
    _write_tar(source, [("manifest.json", b"{" + (b" " * 64) + b"}")])
    monkeypatch.setattr(archive, "MAX_MANIFEST_BYTES", 32)

    assert archive.read_member_bytes(str(source), "manifest.json") is None


def test_manifest_reader_stops_at_member_count_budget(tmp_path, monkeypatch):
    source = tmp_path / "many-before-manifest.tar.gz"
    _write_tar(source, [("one", b"1"), ("two", b"2"), ("manifest.json", b"{}")])
    monkeypatch.setattr(archive, "MAX_ARCHIVE_MEMBERS", 2)

    assert archive.read_member_bytes(str(source), "manifest.json") is None


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [("MAX_MEMBER_BYTES", 31), ("MAX_TOTAL_EXTRACTED_BYTES", 31)],
)
def test_manifest_reader_rejects_oversized_member_before_manifest(
        tmp_path, monkeypatch, limit_name, limit):
    source = tmp_path / "oversized-before-manifest.tar.gz"
    _write_tar(source, [("payload", b"A" * 32), ("manifest.json", b"{}")])
    monkeypatch.setattr(archive, limit_name, limit)

    assert archive.read_member_bytes(str(source), "manifest.json") is None


def test_service_user_must_exist_and_must_not_be_root(monkeypatch):
    with pytest.raises(ValueError, match="invalid service user"):
        service.validate_service_user("www\nRUNAS=root")

    monkeypatch.setattr(
        service.pwd,
        "getpwnam",
        lambda name: pwd.struct_passwd((name, "x", 0, 0, "", "/root", "/bin/sh")),
    )
    with pytest.raises(ValueError, match="non-root"):
        service.validate_service_user("root")

    monkeypatch.setattr(
        service.pwd,
        "getpwnam",
        lambda name: pwd.struct_passwd((name, "x", 1000, 1000, "", "/srv/app", "/bin/sh")),
    )
    assert service.validate_service_user("www") == "www"

    def missing(_name):
        raise KeyError("missing")

    monkeypatch.setattr(service.pwd, "getpwnam", missing)
    with pytest.raises(ValueError, match="does not exist"):
        service.validate_service_user("missing")


@pytest.mark.parametrize(
    ("java_home", "java_opts"),
    [
        ("/safe/java\nRUNAS=root", "-Xmx128m"),
        ("/safe/java", "-Xmx128m\nRUNAS=root"),
        ("/safe/java", "-javaagent:$(touch /tmp/owned)"),
    ],
)
def test_jar_service_rejects_template_control_injection(
        tmp_path, monkeypatch, java_home, java_opts):
    monkeypatch.setattr(service, "validate_service_user", lambda _user: "www")

    with pytest.raises(ValueError, match="unsafe service"):
        service.install_jar_unit(
            "safeapp", java_home, str(tmp_path / "app"), 8123,
            java_opts=java_opts, user="www",
        )


def test_initd_jar_loads_only_app_environment_after_privilege_drop(tmp_path):
    app_dir = tmp_path / "app"
    java_home = tmp_path / "java"
    trusted_bin = tmp_path / "trusted-bin"
    attacker_bin = tmp_path / "attacker-bin"
    for path in (app_dir / "bin", app_dir / "logs", java_home / "bin",
                 trusted_bin, attacker_bin):
        path.mkdir(parents=True, exist_ok=True)

    (app_dir / "app.jar").write_bytes(b"jar")
    (app_dir / "bin" / "app.env").write_text(
        "RUNAS=root\nJAVA_HOME=/attacker/java\nAPP_DIR=/attacker/app\n"
        "JAR=/attacker/app.jar\nJAVA_OPTS=-javaagent:/attacker.jar\n"
        "DB_URL=jdbc:test\nSPRING_PROFILES_ACTIVE=prod\n"
        "CUSTOM_APP_VAR=custom-value\n"
        'QUOTED_VALUE="say \\"hello\\" at C:\\\\apps\\\\demo"\n'
        "9INVALID=ignored\n"
    )
    (java_home / "bin" / "java").write_text(
        "#!/bin/sh\n"
        "if env | grep -q '^9INVALID='; then invalid=present; else invalid=unset; fi\n"
        "printf 'DB_URL=%s\\nSPRING_PROFILES_ACTIVE=%s\\nCUSTOM_APP_VAR=%s\\n"
        "QUOTED_VALUE=%s\\nINVALID=%s\\nPATH=%s\\nLD_PRELOAD=%s\\n"
        "BASH_ENV=%s\\nARGS=%s\\n' "
        '"$DB_URL" "$SPRING_PROFILES_ACTIVE" "$CUSTOM_APP_VAR" '
        '"$QUOTED_VALUE" "$invalid" "$PATH" "${LD_PRELOAD-unset}" '
        '"${BASH_ENV-unset}" "$*"\n'
    )
    os.chmod(java_home / "bin" / "java", 0o755)

    runas_record = tmp_path / "runas.txt"
    trusted_runuser = trusted_bin / "runuser"
    trusted_runuser.write_text(
        "#!/bin/sh\n"
        'printf "%%s" "$2" > %s\n' % str(runas_record)
        + "shift 3\n"
        + 'exec "$@"\n'
    )
    os.chmod(trusted_runuser, 0o755)
    poison_marker = tmp_path / "path-poisoned"
    for command in ("runuser", "env", "bash", "kill", "rm"):
        path = attacker_bin / command
        path.write_text("#!/bin/sh\necho poisoned > %s\nexit 99\n" % poison_marker)
        os.chmod(path, 0o755)
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("echo poisoned > %s\n" % poison_marker)

    rendered = templating.render_file("initd-jar.sh.tmpl", {
        "app": "safeapp", "user": "www", "group": "www",
        "java_home": str(java_home), "app_dir": str(app_dir),
        "port": "8123", "java_opts": "-Xms64m -Xmx128m",
    })
    assert rendered.startswith("#!/bin/bash -p\n")
    assert "/usr/bin/env -i" in rendered
    assert "/usr/sbin/runuser" in rendered and "/bin/bash" in rendered
    script = tmp_path / "service"
    script.write_text(rendered.replace("/usr/sbin/runuser", str(trusted_runuser)))
    os.chmod(script, 0o755)
    env = dict(os.environ)
    env["PATH"] = str(attacker_bin)
    env["BASH_ENV"] = str(bash_env)
    env["LD_PRELOAD"] = "/definitely/not/a/real/javahost-test.so"

    subprocess.run([str(script), "start"], env=env, check=True)
    output = app_dir / "logs" / "app.out"
    for _ in range(100):
        if output.exists() and "ARGS=" in output.read_text():
            break
        time.sleep(0.01)

    assert runas_record.read_text() == "www"
    body = output.read_text()
    assert "DB_URL=jdbc:test" in body
    assert "SPRING_PROFILES_ACTIVE=prod" in body
    assert "CUSTOM_APP_VAR=custom-value" in body
    assert 'QUOTED_VALUE=say "hello" at C:\\apps\\demo' in body
    assert "INVALID=unset" in body
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in body
    assert "LD_PRELOAD=unset" in body and "BASH_ENV=unset" in body
    assert "-Xms64m -Xmx128m -jar" in body
    assert "/attacker" not in body
    assert not poison_marker.exists()

    runas_record.unlink()
    (app_dir / "app.pid").write_text("999999\n")
    subprocess.run([str(script), "stop"], env=env, check=True)
    assert runas_record.read_text() == "www"
    assert not poison_marker.exists()
