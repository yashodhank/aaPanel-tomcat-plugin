# coding: utf-8
"""Offline tests for backup/restore (core/backup). No systemd, nginx, certbot or
/etc — INSTANCE_ROOT/BACKUPS_ROOT are redirected to tmp and the service/proxy/ssl
boundaries are stubbed. Focus: tar-traversal safety, round-trip restore-as-new
(reallocated port, excluded logs, no /etc/letsencrypt), and best-effort SSL."""
import io
import json
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
    java_home = str(tmp_path / "runtimes" / "jdk-17")
    tomcat_home = str(tmp_path / "tomcat" / "10")
    systemd_dir = str(tmp_path / "systemd")
    initd_dir = str(tmp_path / "initd")
    os.makedirs(iroot, exist_ok=True)
    os.makedirs(java_home, exist_ok=True)
    os.makedirs(tomcat_home, exist_ok=True)
    os.makedirs(systemd_dir, exist_ok=True)
    os.makedirs(initd_dir, exist_ok=True)
    monkeypatch.setattr(instance, "INSTANCE_ROOT", iroot)
    monkeypatch.setattr(store, "BACKUPS_ROOT", broot)
    monkeypatch.setattr(store.java, "detect", lambda: {17: java_home})
    monkeypatch.setattr(store.installer, "home_path", lambda major: tomcat_home)
    monkeypatch.setattr(store.installer, "is_installed", lambda major: "10.1-test")
    monkeypatch.setattr(store.service, "SYSTEMD_DIR", systemd_dir)
    monkeypatch.setattr(store.service, "INITD_DIR", initd_dir)
    # allow safe_rmtree/mark under the tmp roots
    monkeypatch.setattr(fs, "MANAGED_ROOTS", tuple(fs.MANAGED_ROOTS) + (iroot, broot))
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
    parent = os.path.dirname(iroot)
    java_home = os.path.join(parent, "runtimes", "jdk-17")
    tomcat_home = os.path.join(parent, "tomcat", "10")
    with open(os.path.join(base, "conf", "server.xml"), "w") as f:
        f.write('<Server><Service><Connector port="%d" protocol="HTTP/1.1"/></Service></Server>' % port)
    with open(os.path.join(base, "bin", "setenv.sh"), "w") as f:
        f.write('export JAVA_HOME="%s"\nexport CATALINA_HOME="%s"\nexport JAVA_OPTS="-Xmx512m"\n'
                % (java_home, tomcat_home))
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write('DB_URL="jdbc:postgresql://h:5432/d"\nDB_USER="appuser"\nDB_PASSWORD="s3cret"\n')
    with open(os.path.join(base, "webapps", "ROOT", "index.jsp"), "w") as f:
        f.write("PAYLOAD-OK")
    with open(os.path.join(base, "logs", "catalina.out"), "w") as f:
        f.write("noise")
    return base


def _mk_jar(iroot, app, port=8080, opts="-Xms128m -Xmx512m"):
    base = os.path.join(iroot, app)
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    os.makedirs(os.path.join(base, "logs"), exist_ok=True)
    fs.mark_managed(base)
    java_home = os.path.join(os.path.dirname(iroot), "runtimes", "jdk-17")
    with open(os.path.join(base, "app.jar"), "wb") as f:
        f.write(b"jar-payload")
    with open(os.path.join(base, "bin", "app.env"), "w") as f:
        f.write("SERVER_PORT=%d\nJAVA_HOME=%s\nJAVA_OPTS=%s\n" % (port, java_home, opts))
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


def test_restore_overwrite_validates_payload_before_removing_live_app(env, tmp_path, monkeypatch):
    """A structurally incomplete archive must not disturb the running instance."""
    import json

    iroot, _ = env
    live = _mk_app(iroot, "live", port=8090)
    original = os.path.join(live, "webapps", "ROOT", "index.jsp")
    man = json.dumps({"app": "live", "type": "war", "format": 1}).encode()
    bad = str(tmp_path / "missing-base.tar.gz")
    with tarfile.open(bad, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(man)
        tf.addfile(ti, io.BytesIO(man))
    monkeypatch.setattr(store.service, "remove_unit",
                        lambda app: (_ for _ in ()).throw(AssertionError("original unit removed")))

    with pytest.raises(RuntimeError, match="missing base"):
        store.restore(bad)

    assert instance.exists("live")
    assert open(original).read() == "PAYLOAD-OK"


def test_restore_overwrite_rolls_back_live_app_when_service_rebuild_fails(env, monkeypatch):
    """A failure after cutover must restore the original managed instance tree."""
    iroot, _ = env
    live = _mk_app(iroot, "live", port=8090)
    original = os.path.join(live, "webapps", "ROOT", "index.jsp")
    arc = store.backup_app("live")["archive"]
    with open(original, "w") as f:
        f.write("LIVE-NEWER-THAN-BACKUP")

    calls = {"install": 0}

    def fail_first_install(*args, **kwargs):
        calls["install"] += 1
        if calls["install"] == 1:
            raise RuntimeError("service rebuild failed")
        return "/unit"

    monkeypatch.setattr(store.service, "install_unit", fail_first_install)

    with pytest.raises(RuntimeError, match="service rebuild failed"):
        store.restore(arc)

    assert instance.exists("live")
    assert fs.is_managed(live)
    assert open(original).read() == "LIVE-NEWER-THAN-BACKUP"
    assert calls["install"] == 1  # rollback keeps/restores the original unit instead of re-rendering it
    assert os.listdir(iroot) == ["live"]


def test_restore_overwrite_replaces_live_tree_and_removes_recovery(env):
    iroot, _ = env
    live = _mk_app(iroot, "live", port=8090)
    original = os.path.join(live, "webapps", "ROOT", "index.jsp")
    arc = store.backup_app("live")["archive"]
    with open(original, "w") as f:
        f.write("LIVE-NEWER-THAN-BACKUP")

    res = store.restore(arc)

    assert res["restored"] is True and res["mode"] == "overwrite"
    assert open(original).read() == "PAYLOAD-OK"
    assert os.listdir(iroot) == ["live"]


@pytest.mark.parametrize("bad_opt", [
    '-Xmx512m;touch/tmp/pwned',
    '-Xmx512m$(touch/tmp/pwned)',
    '-Xmx512m`touch/tmp/pwned`',
    '-Xmx512m"\nExecStart=/bin/sh',
])
def test_restore_rejects_hostile_jvm_opts_before_rendering(env, monkeypatch, bad_opt):
    iroot, _ = env
    base = _mk_app(iroot, "src", port=8090)
    setenv = os.path.join(base, "bin", "setenv.sh")
    body = open(setenv).read().replace('-Xmx512m"', bad_opt + '"')
    with open(setenv, "w") as f:
        f.write(body)
    arc = store.backup_app("src")["archive"]
    monkeypatch.setattr(store.service, "install_unit",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unsafe config rendered")))
    with pytest.raises(ValueError, match="archived"):
        store.restore(arc, as_name="clone")
    assert not instance.exists("clone")


@pytest.mark.parametrize("field,value", [("type", "../../systemd"), ("port", "not-a-port")])
def test_restore_rejects_invalid_manifest_contract(env, tmp_path, field, value):
    iroot, _ = env
    _mk_app(iroot, "src", port=8090)
    arc = store.backup_app("src")["archive"]
    unpacked = str(tmp_path / ("unpacked-" + field))
    archive.safe_extract_tar(arc, unpacked)
    manifest_path = os.path.join(unpacked, "manifest.json")
    man = json.loads(open(manifest_path).read())
    man[field] = value
    with open(manifest_path, "w") as f:
        json.dump(man, f)
    tampered = str(tmp_path / ("tampered-%s.tar.gz" % field))
    archive.pack([(manifest_path, "manifest.json"),
                  (os.path.join(unpacked, "base"), "base")], tampered)
    with pytest.raises(ValueError):
        store.restore(tampered, as_name="clone")
    assert not instance.exists("clone")


def test_restore_refuses_symlink_target_even_when_marker_resolves(env):
    iroot, _ = env
    _mk_app(iroot, "src", port=8090)
    arc = store.backup_app("src")["archive"]
    os.symlink(instance.base_path("src"), instance.base_path("clone"))
    with pytest.raises(RuntimeError, match="symlink"):
        store.restore(arc, as_name="clone")


def test_restore_stop_failure_leaves_live_tree_untouched(env, monkeypatch):
    iroot, _ = env
    live = _mk_app(iroot, "live", port=8090)
    original = os.path.join(live, "webapps", "ROOT", "index.jsp")
    arc = store.backup_app("live")["archive"]
    with open(original, "w") as f:
        f.write("LIVE-NEWER")
    monkeypatch.setattr(store.service, "status", lambda app: "active")
    monkeypatch.setattr(store.service, "action",
                        lambda app, what: (_ for _ in ()).throw(RuntimeError("stop failed")))
    with pytest.raises(RuntimeError, match="stop failed"):
        store.restore(arc)
    assert open(original).read() == "LIVE-NEWER"


def test_restore_overwrite_preserves_inactive_state_and_hides_transaction(env, monkeypatch):
    iroot, _ = env
    _mk_app(iroot, "live", port=8090)
    arc = store.backup_app("live")["archive"]
    seen = []

    def install(*args, **kwargs):
        seen.append(sorted(os.listdir(iroot)))
        return "/unit"

    monkeypatch.setattr(store.service, "install_unit", install)
    monkeypatch.setattr(store.service, "enable_start",
                        lambda app: (_ for _ in ()).throw(AssertionError("inactive app was started")))
    store.restore(arc)
    assert seen == [["live"]]


def test_per_app_restore_lock_rejects_overlap(env):
    with store._app_restore_lock("live"):
        with pytest.raises(RuntimeError, match="already in progress"):
            with store._app_restore_lock("live"):
                pass


def test_restore_jar_preserves_opts_and_rolls_back_tree(env, monkeypatch):
    iroot, _ = env
    live = _mk_jar(iroot, "live", port=8090, opts="-Xms128m -Xmx768m")
    arc = store.backup_app("live")["archive"]
    with open(os.path.join(live, "app.jar"), "wb") as f:
        f.write(b"newer-live-jar")
    captured = []

    def fail_install(*args, **kwargs):
        captured.append(kwargs.get("java_opts"))
        raise RuntimeError("jar unit failed")

    monkeypatch.setattr(store.service, "install_jar_unit", fail_install)
    with pytest.raises(RuntimeError, match="jar unit failed"):
        store.restore(arc)
    assert captured == ["-Xms128m -Xmx768m"]
    assert open(os.path.join(live, "app.jar"), "rb").read() == b"newer-live-jar"


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
