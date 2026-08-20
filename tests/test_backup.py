# coding: utf-8
"""Offline tests for backup/restore (core/backup). No systemd, nginx, certbot or
/etc — INSTANCE_ROOT/BACKUPS_ROOT are redirected to tmp and the service/proxy/ssl
boundaries are stubbed. Focus: tar-traversal safety, round-trip restore-as-new
(reallocated port, excluded logs, no /etc/letsencrypt), and best-effort SSL."""
import io
import os
import tarfile

import pytest

from core.backup import archive, store
from core.backup.archive import UnsafeArchive
from core.tomcat import instance
from core.util import fs


# --------------------------------------------------------------------------- #
# tar-traversal / link / device safety  (the untrusted-input boundary)
# --------------------------------------------------------------------------- #
def _tar_with(path, build):
    with tarfile.open(path, "w:gz") as tf:
        build(tf)


def _reg(name, data=b"x"):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    return ti, io.BytesIO(data)


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "hardlink", "device"])
def test_safe_extract_rejects_dangerous_members(tmp_path, kind):
    tp = str(tmp_path / ("bad-%s.tar.gz" % kind))

    def build(tf):
        if kind == "traversal":
            ti, f = _reg("../escape.txt"); tf.addfile(ti, f)
        elif kind == "absolute":
            ti, f = _reg("/etc/passwd"); tf.addfile(ti, f)
        elif kind == "symlink":
            ti = tarfile.TarInfo("evil"); ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"; tf.addfile(ti)
        elif kind == "hardlink":
            ti0, f = _reg("real"); tf.addfile(ti0, f)
            ti = tarfile.TarInfo("link"); ti.type = tarfile.LNKTYPE; ti.linkname = "real"; tf.addfile(ti)
        elif kind == "device":
            ti = tarfile.TarInfo("dev"); ti.type = tarfile.CHRTYPE; ti.devmajor = 1; ti.devminor = 3; tf.addfile(ti)

    _tar_with(tp, build)
    with pytest.raises(UnsafeArchive):
        archive.safe_extract_tar(tp, str(tmp_path / "out"))


def test_pack_extract_round_trip(tmp_path):
    src = tmp_path / "src"; (src / "d").mkdir(parents=True)
    (src / "d" / "f.txt").write_text("hello-payload")
    dest = str(tmp_path / "a.tar.gz")
    archive.pack([(str(src / "d"), "base/d")], dest)
    out = str(tmp_path / "out")
    archive.safe_extract_tar(dest, out)
    assert open(os.path.join(out, "base", "d", "f.txt")).read() == "hello-payload"


# --------------------------------------------------------------------------- #
# fixtures for backup/restore over a fake instance store
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path, monkeypatch):
    iroot = str(tmp_path / "instances")
    broot = str(tmp_path / "backups")
    os.makedirs(iroot, exist_ok=True)
    monkeypatch.setattr(instance, "INSTANCE_ROOT", iroot)
    monkeypatch.setattr(store, "BACKUPS_ROOT", broot)
    # allow safe_rmtree/mark under the tmp roots
    monkeypatch.setattr(fs, "MANAGED_ROOTS", (iroot, broot))
    # stub the live boundaries
    monkeypatch.setattr(store.service, "status", lambda app: "inactive")
    monkeypatch.setattr(store.service, "action", lambda app, what: None)
    monkeypatch.setattr(store.service, "remove_unit", lambda app: None)
    monkeypatch.setattr(store.service, "enable_start", lambda app: None)
    monkeypatch.setattr(store.service, "install_unit", lambda *a, **k: "/unit")
    monkeypatch.setattr(store.service, "install_jar_unit", lambda *a, **k: "/unit")
    monkeypatch.setattr(store.service, "write_setenv", lambda *a, **k: "/setenv")
    monkeypatch.setattr(store.instance, "allocate_port", lambda preferred=None: 8123)
    monkeypatch.setattr(store.proxy, "write_vhost", lambda *a, **k: "/vhost")
    monkeypatch.setattr(store.proxy, "ensure_include", lambda *a, **k: True)
    monkeypatch.setattr(store.proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(store.proxy, "_store_domain", lambda app, dom: None)
    from core.util import shell
    monkeypatch.setattr(shell, "run", lambda *a, **k: (0, "", ""))
    return iroot, broot


def _mk_app(iroot, app, port=8080):
    base = os.path.join(iroot, app)
    for d in ("conf", os.path.join("webapps", "ROOT"), "bin", "logs", "work", "temp"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    fs.mark_managed(base)
    with open(os.path.join(base, "conf", "server.xml"), "w") as f:
        f.write('<Server><Service><Connector port="%d" protocol="HTTP/1.1"/></Service></Server>' % port)
    with open(os.path.join(base, "bin", "setenv.sh"), "w") as f:
        f.write('export JAVA_HOME="/opt/jdk-17"\nexport CATALINA_HOME="/opt/tomcat/10"\nexport JAVA_OPTS="-Xmx512m"\n')
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write('DB_URL="jdbc:postgresql://h:5432/d"\nDB_USER="appuser"\nDB_PASSWORD="s3cret"\n')
    os.chmod(os.path.join(base, "bin", "app.env"), 0o640)
    with open(os.path.join(base, "webapps", "ROOT", "index.jsp"), "w") as f:
        f.write("PAYLOAD-OK")
    with open(os.path.join(base, "logs", "catalina.out"), "w") as f:
        f.write("noise")
    return base


def test_backup_manifest_and_exclusions(env):
    iroot, broot = env
    _mk_app(iroot, "myapp", port=8090)
    res = store.backup_app("myapp")
    assert os.path.isfile(res["archive"])
    # archive perms 0600 (carries DB creds)
    assert oct(os.stat(res["archive"]).st_mode & 0o777) == "0o600"
    with tarfile.open(res["archive"]) as tf:
        names = tf.getnames()
    assert "manifest.json" in names
    assert any(n.startswith("base/conf") for n in names)
    assert any(n.startswith("base/webapps") for n in names)
    assert any(n.endswith("bin/app.env") for n in names)        # creds included by design
    assert not any(n.startswith("base/logs") for n in names)    # logs excluded
    assert not any("letsencrypt" in n for n in names)           # no private keys ever
    man = store._read_manifest_file(res["archive"])
    assert man["app"] == "myapp" and man["type"] == "war"
    assert man["port"] == 8090 and man["db_engine"] == "postgresql"
    assert man["memory_mb"] == 512


def test_restore_as_new_round_trip(env):
    iroot, broot = env
    _mk_app(iroot, "src", port=8090)
    arc = store.backup_app("src")["archive"]
    res = store.restore(arc, as_name="clone")
    assert res["restored"] and res["mode"] == "new"
    assert res["port"] == 8123                                  # reallocated, not 8090
    base = instance.base_path("clone")
    assert os.path.isfile(os.path.join(base, "webapps", "ROOT", "index.jsp"))
    assert open(os.path.join(base, "webapps", "ROOT", "index.jsp")).read() == "PAYLOAD-OK"
    assert fs.is_managed(base)                                   # marker re-applied
    assert not os.path.isdir(os.path.join(base, "logs"))        # logs were never archived
    assert not os.path.exists(os.path.join(base, "bin", "site.domain"))  # site dropped (no domain)
    # server.xml now points at the reallocated port
    sx = open(os.path.join(base, "conf", "server.xml")).read()
    assert 'port="8123"' in sx and 'port="8090"' not in sx


def test_restore_ssl_reissue_is_best_effort(env, monkeypatch):
    iroot, broot = env
    base = _mk_app(iroot, "src", port=8090)
    # mark the source as SSL-enabled so the manifest records ssl_enabled=True
    with open(os.path.join(base, "bin", "site.ssl"), "w") as f:
        f.write("2030-01-01T00:00:00Z\n")
    with open(os.path.join(base, "bin", "site.domain"), "w") as f:
        f.write("src.example.com\n")
    arc = store.backup_app("src")["archive"]

    def _boom(*a, **k):
        raise RuntimeError("ACME rate-limited")
    monkeypatch.setattr(store.ssl, "enable", _boom)
    res = store.restore(arc, as_name="clone", domain="clone.example.com")
    assert res["restored"] is True
    assert res["ssl"] is False           # re-issue failed but restore still succeeded
    assert "ssl_warning" in res


def test_restore_rejects_malicious_archive(env, tmp_path):
    """An uploaded archive with a path-traversal member must be refused by the
    same restore path the upload endpoint uses (defense = safe_extract_tar)."""
    import json
    man = json.dumps({"app": "x", "type": "war", "format": 1}).encode()
    bad = str(tmp_path / "evil.tar.gz")
    with tarfile.open(bad, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json"); ti.size = len(man); tf.addfile(ti, io.BytesIO(man))
        ev = b"pwn"; ti2 = tarfile.TarInfo("base/../../escape"); ti2.size = len(ev); tf.addfile(ti2, io.BytesIO(ev))
    with pytest.raises(UnsafeArchive):
        store.restore(bad, as_name="clone")
    assert not instance.exists("clone")          # nothing left half-created


def test_restore_forces_secret_modes(env, monkeypatch):
    """After extract, archive must be 0600 and bin/app.env 0640."""
    iroot, broot = env
    base = _mk_app(iroot, "sec", port=8090)
    envp = os.path.join(base, "bin", "app.env")
    # Build the manifest while the source satisfies the privileged app.env
    # reader, then loosen only the archived payload to model an old/untrusted
    # backup carrying permissive member modes.
    manifest = store._build_manifest("sec", base)
    monkeypatch.setattr(store, "_build_manifest",
                        lambda app, app_base: manifest)
    os.chmod(envp, 0o666)  # deliberately loose before backup
    arc = store.backup_app("sec")["archive"]
    # loosen archive mode to simulate a bad upload umask
    os.chmod(arc, 0o644)
    store.restore(arc, as_name="sec2")
    assert oct(os.stat(arc).st_mode & 0o777) == "0o600"
    restored = os.path.join(instance.base_path("sec2"), "bin", "app.env")
    assert oct(os.stat(restored).st_mode & 0o777) == "0o640"


def test_restore_jar_passes_java_opts_from_manifest(env, monkeypatch):
    """JAR restore must not reinstall the unit with empty java_opts."""
    iroot, broot = env
    app = "jarapp"
    base = os.path.join(iroot, app)
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    fs.mark_managed(base)
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write("SERVER_PORT=8099\nJAVA_HOME=/opt/jdk-17\n"
                "SERVER_ADDRESS=127.0.0.1\nSERVER_HOST=127.0.0.1\n")
    os.chmod(os.path.join(base, "bin", "app.env"), 0o640)
    with open(os.path.join(base, "app.jar"), "wb") as f:
        f.write(b"PK\x05\x06" + b"\0" * 18)  # minimal zip EOCD
    # stub _app_info so manifest builds as jar with memory
    monkeypatch.setattr(instance, "_app_info",
                        lambda a: {"type": "jar", "tomcat": None, "java": 17,
                                   "port": 8099, "domain": None, "ssl": False})
    monkeypatch.setattr(store, "_read_unit_java_opts",
                        lambda a: "-server -Xmx256m -XX:+UseG1GC")
    seen = {}

    def capture_jar_unit(app, java_home, app_dir, port, java_opts="", user="www"):
        seen["java_opts"] = java_opts
        return "/unit"

    monkeypatch.setattr(store.service, "install_jar_unit", capture_jar_unit)
    arc = store.backup_app(app)["archive"]
    man = store._read_manifest_file(arc)
    assert man.get("java_opts")
    assert man.get("memory_mb") == 256
    store.restore(arc, as_name="jarclone")
    assert "-Xmx256m" in seen.get("java_opts", "")


def test_backup_jar_prefers_app_env_java_opts_over_unit(env, monkeypatch):
    """The persisted app contract outranks a possibly missing/stale unit."""
    iroot, _broot = env
    app = "jarenv"
    base = os.path.join(iroot, app)
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    fs.mark_managed(base)
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write("SERVER_PORT=8099\nJAVA_HOME=/opt/jdk-17\n"
                'JAVA_OPTS="-server -Xmx768m -XX:+UseG1GC"\n')
    os.chmod(os.path.join(base, "bin", "app.env"), 0o640)
    with open(os.path.join(base, "app.jar"), "wb") as f:
        f.write(b"PK\x05\x06" + b"\0" * 18)
    monkeypatch.setattr(instance, "_app_info",
                        lambda a: {"type": "jar", "tomcat": None, "java": 17,
                                   "port": 8099, "domain": None, "ssl": False})
    monkeypatch.setattr(store, "_read_unit_java_opts",
                        lambda a: "-server -Xmx256m")

    manifest = store._build_manifest(app, base)

    assert manifest["java_opts"] == "-server -Xmx768m -XX:+UseG1GC"
    assert manifest["memory_mb"] == 768


def test_restore_java_opts_prefers_app_env_before_manifest(env, monkeypatch):
    iroot, _broot = env
    base = os.path.join(iroot, "jaropts")
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    fs.mark_managed(base)
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write('JAVA_OPTS="-server -Xmx640m -XX:+UseG1GC"\n')
    os.chmod(os.path.join(base, "bin", "app.env"), 0o640)

    opts = store._restore_java_opts(
        base, "jar", {"java_opts": "-Xmx128m", "memory_mb": 256},
        "/opt/jdk-17")

    assert opts == "-server -Xmx640m -XX:+UseG1GC"


@pytest.mark.parametrize("source", ["setenv", "app_env", "manifest"])
def test_restore_java_opts_sanitizes_every_archive_source(env, monkeypatch,
                                                          source):
    """Untrusted backup options must be safe before service template render."""
    iroot, _broot = env
    base = os.path.join(iroot, "hostileopts")
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    fs.mark_managed(base)
    hostile = ('-server -Xmx128m"; /usr/bin/id; # '
               '$(touch /tmp/javahost-owned)\n-Dsafe=yes')
    manifest = {"memory_mb": 256}
    monkeypatch.setattr(instance, "_read_setenv", lambda _base: {})
    monkeypatch.setattr(instance, "_read_app_env", lambda _base: {})
    if source == "setenv":
        monkeypatch.setattr(instance, "_read_setenv",
                            lambda _base: {"JAVA_OPTS": hostile})
    elif source == "app_env":
        monkeypatch.setattr(instance, "_read_app_env",
                            lambda _base: {"JAVA_OPTS": hostile})
    else:
        manifest["java_opts"] = hostile

    opts = store._restore_java_opts(base, "jar", manifest, "/opt/jdk-17")

    assert opts == "-server -Dsafe=yes"
    assert all(token not in opts for token in
               ('"', ";", "$(", "\n", "/usr/bin/id", "javahost-owned"))


def test_restore_hostile_jar_archive_never_renders_shell_tokens(env,
                                                               monkeypatch):
    """The restore-to-service boundary receives only sanitized archive opts."""
    iroot, _broot = env
    app = "hostilejar"
    base = os.path.join(iroot, app)
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    fs.mark_managed(base)
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write('SERVER_PORT="8099"\nJAVA_HOME="/opt/jdk-17"\n'
                'JAVA_OPTS="-server -Xmx128m\\"; /usr/bin/id; #"\n')
    os.chmod(os.path.join(base, "bin", "app.env"), 0o640)
    with open(os.path.join(base, "app.jar"), "wb") as f:
        f.write(b"PK\x05\x06" + b"\0" * 18)
    monkeypatch.setattr(instance, "_app_info",
                        lambda _app: {"type": "jar", "tomcat": None,
                                      "java": 17, "port": 8099,
                                      "domain": None, "ssl": False})
    seen = {}

    def capture_jar_unit(_app, _java_home, _app_dir, _port, java_opts="",
                         user="www"):
        seen["java_opts"] = java_opts
        return "/unit"

    monkeypatch.setattr(store.service, "install_jar_unit", capture_jar_unit)

    archive_path = store.backup_app(app)["archive"]
    store.restore(archive_path, as_name="safeclone")

    assert seen["java_opts"] == "-server"
    assert all(token not in seen["java_opts"] for token in
               ('"', ";", "$(", "/usr/bin/id"))


def test_secure_restored_secrets_fails_closed(env, monkeypatch):
    iroot, _broot = env
    base = os.path.join(iroot, "secret")
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    envp = os.path.join(base, "bin", "app.env")
    open(envp, "w").write("DB_PASSWORD=x\n")
    real_chmod = os.chmod

    def deny_env(path, mode):
        if path == envp:
            raise PermissionError("chmod denied")
        return real_chmod(path, mode)

    monkeypatch.setattr(store.os, "chmod", deny_env)

    with pytest.raises(PermissionError, match="chmod denied"):
        store._secure_restored_secrets(base)


def test_restore_discards_app_when_secret_permissions_fail(env, monkeypatch):
    iroot, _broot = env
    _mk_app(iroot, "source", port=8090)
    archive_path = store.backup_app("source")["archive"]
    real_chmod = os.chmod

    def deny_restored_env(path, mode):
        if path.endswith("/clone/bin/app.env"):
            raise PermissionError("chmod denied")
        return real_chmod(path, mode)

    monkeypatch.setattr(store.os, "chmod", deny_restored_env)

    with pytest.raises(PermissionError, match="chmod denied"):
        store.restore(archive_path, as_name="clone")

    assert not instance.exists("clone")


def test_overwrite_restore_checks_secret_modes_before_deleting_live_app(env, monkeypatch):
    iroot, _broot = env
    base = _mk_app(iroot, "live", port=8090)
    payload = os.path.join(base, "webapps", "ROOT", "index.jsp")
    open(payload, "w").write("ORIGINAL-LIVE")
    archive_path = store.backup_app("live")["archive"]
    real_chmod = os.chmod

    def deny_staged_env(path, mode):
        if path.endswith("/base/bin/app.env"):
            raise PermissionError("chmod denied")
        return real_chmod(path, mode)

    monkeypatch.setattr(store.os, "chmod", deny_staged_env)

    with pytest.raises(PermissionError, match="chmod denied"):
        store.restore(archive_path)

    assert instance.exists("live")
    assert open(payload).read() == "ORIGINAL-LIVE"


def test_delete_backup_refuses_escape(env):
    with pytest.raises(ValueError):
        store.delete_backup("../../etc/passwd")
    with pytest.raises(ValueError):
        store.delete_backup("not-a-valid-name.txt")


def test_prune_keeps_newest(env, monkeypatch):
    iroot, broot = env
    _mk_app(iroot, "app", port=8090)
    # fabricate three backups with distinct timestamps via the manifest order
    names = []
    import time as _t
    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]
    seq = iter(stamps)
    monkeypatch.setattr(store, "_now_stamp", lambda: next(seq))
    # created_at drives sort; make it match the stamp order
    cre = iter(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"])
    monkeypatch.setattr(store, "_now_iso", lambda: next(cre))
    for _ in range(3):
        names.append(store.backup_app("app")["name"])
    out = store.prune_backups("app", keep=2)
    assert len(out["removed"]) == 1
    remaining = {b["name"] for b in store.list_backups("app")}
    assert len(remaining) == 2
    assert names[0] not in remaining   # oldest pruned


# --------------------------------------------------------------------------- #
# v0.20.0 — sidecar manifest, backup_dest, multi-destination
# --------------------------------------------------------------------------- #
def test_sidecar_written_and_used(env, monkeypatch):
    iroot, broot = env
    _mk_app(iroot, "myapp", port=8090)
    res = store.backup_app("myapp")
    side = res["archive"] + ".json"
    assert os.path.isfile(side)                                  # sidecar written
    import json
    man = json.loads(open(side).read())
    assert man["app"] == "myapp" and man["uploaded_to"] == []
    # listing must use the sidecar, NOT open the tarball
    monkeypatch.setattr(store.archive, "read_member_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("tar opened despite sidecar")))
    rows = store.list_backups("myapp")
    assert rows and rows[0]["app"] == "myapp" and rows[0]["locations"] == ["local"]


def test_backup_dest_configurable(env, monkeypatch, tmp_path):
    iroot, broot = env
    custom = str(tmp_path / "custom-backups")
    monkeypatch.setattr(store.config, "get", lambda k, d=None: custom if k == "backup_dest" else d)
    monkeypatch.setattr(store.fs, "MANAGED_ROOTS", tuple(store.fs.MANAGED_ROOTS) + (custom,))
    _mk_app(iroot, "myapp", port=8090)
    res = store.backup_app("myapp")
    assert res["archive"].startswith(custom + os.sep) and os.path.isfile(res["archive"])


def test_backup_multi_destination(env, monkeypatch):
    iroot, broot = env
    _mk_app(iroot, "myapp", port=8090)
    from core.backup import remote as remotemod
    monkeypatch.setattr(remotemod, "_resolve_ids", lambda ids: ["w", "m"])
    monkeypatch.setattr(remotemod, "upload",
                        lambda dest, name, ids: {"results": {"w": {"ok": True}, "m": {"ok": False, "detail": "boom"}},
                                                 "ok_ids": ["w"]})
    res = store.backup_app("myapp", remotes="w,m")
    assert res["uploaded_to"] == ["w"]
    assert res["locations"] == ["local", "w"]
    assert res["upload_results"]["m"]["ok"] is False          # partial failure surfaced
    # sidecar records where it actually landed
    import json
    assert json.loads(open(res["archive"] + ".json").read())["uploaded_to"] == ["w"]


def test_list_backups_locations_merge(env, monkeypatch):
    iroot, broot = env
    _mk_app(iroot, "myapp", port=8090)
    name = store.backup_app("myapp")["name"]
    from core.backup import remote as remotemod
    monkeypatch.setattr(remotemod, "enabled_ids", lambda: ["wasabi"])
    monkeypatch.setattr(remotemod, "list_remote",
                        lambda pid: [{"name": name, "app": "myapp", "size_bytes": 10, "size_mb": 0.0}])
    rows = store.list_backups("myapp", include_remote=True)
    assert rows[0]["locations"] == ["local", "wasabi"]


def test_delete_backup_local_only(env, monkeypatch):
    iroot, broot = env
    _mk_app(iroot, "myapp", port=8090)
    res = store.backup_app("myapp")
    from core.backup import remote as remotemod
    called = {"n": 0}
    monkeypatch.setattr(remotemod, "configured", lambda: True)
    monkeypatch.setattr(remotemod, "delete", lambda name, ids=None: called.__setitem__("n", called["n"] + 1) or {"removed_from": []})
    out = store.delete_backup(res["name"], locations=["local"])
    assert out["removed"] is True
    assert not os.path.isfile(res["archive"]) and not os.path.isfile(res["archive"] + ".json")
    assert called["n"] == 0                                   # remote delete NOT called for local-only
