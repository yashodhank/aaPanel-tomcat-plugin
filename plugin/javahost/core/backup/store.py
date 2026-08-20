# coding: utf-8
"""
Local backup store + restore for JavaHost apps.

An archive captures everything needed to recreate an app EXCEPT regenerable /
sensitive externals:
  IN  : manifest.json, base/conf, base/webapps, base/bin (setenv.sh, app.env,
        site.domain, site.ssl), base/app.jar (jar apps), nginx/<app>.conf
  OUT : logs/ work/ temp/, the systemd/init.d unit (re-rendered on restore — we
        never unpack an executable unit), and ALL of /etc/letsencrypt (private
        keys are never bundled; SSL is RE-ISSUED on restore, best-effort).

Restore has two modes:
  * overwrite (as_name=None): fully stage and validate the replacement, then
    swap it into place while retaining the original tree for rollback.
  * restore-as-new (as_name set): reallocate the port, rewrite server.xml/app.env,
    remap (or drop) the domain so two apps never collide.

Backups contain DB credentials (bin/app.env), so archives are written 0600 under
the managed backups dir. Names are strictly validated; every path is realpath-
contained. Defensive throughout.
"""
from __future__ import annotations

import json
import fcntl
import os
import re
import shlex
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from typing import Dict, List, Optional

from .. import config
from ..deploy import proxy, ssl
from ..runtime import java, jvm_opts
from ..tomcat import installer, instance, service, templating
from ..util import shell
from ..util import fs, validate
from . import archive

BACKUPS_ROOT = "/www/server/javahost/backups"   # default; override via config "backup_dest"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1


def _restore_root() -> str:
    """Private same-filesystem restore workspace, never visible as an app."""
    return os.path.join(os.path.dirname(instance.INSTANCE_ROOT), ".restore-transactions")


@contextmanager
def _app_restore_lock(app: str):
    root = _restore_root()
    fs.ensure_dir(root, 0o700)
    fs.mark_managed(root)
    locks = fs.ensure_dir(os.path.join(root, "locks"), 0o700)
    path = os.path.join(locks, "%s.lock" % validate.identifier(app, "app"))
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("restore already in progress for app: %s" % app)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _backups_root() -> str:
    """Local backups dir — `config.get('backup_dest')` (absolute) or the default."""
    d = config.get("backup_dest")
    if d and os.path.isabs(str(d)):
        return str(d)
    return BACKUPS_ROOT


def _sidecar(path: str) -> str:
    """Path of an archive's sidecar manifest (`<archive>.json`) for cheap listing."""
    return path + ".json"

# backup-<app>-<YYYYmmddTHHMMSSZ>.tar.gz
_NAME_RE = re.compile(r"^backup-[A-Za-z0-9._-]+-\d{8}T\d{6}Z\.tar\.gz$")
_APP_FROM_NAME = re.compile(r"^backup-(?P<app>[A-Za-z0-9._-]+)-\d{8}T\d{6}Z\.tar\.gz$")


def _app_from_name(name: str) -> Optional[str]:
    m = _APP_FROM_NAME.match(name or "")
    return m.group("app") if m else None
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _plugin_version() -> str:
    try:
        with open(os.path.join(_PLUGIN_DIR, "info.json")) as f:
            return str(json.load(f).get("versions", "")) or "unknown"
    except Exception:
        return "unknown"


def _backup_path(name: str) -> str:
    """Realpath-contained path for a backup file name under the backups dir."""
    if not _NAME_RE.match(name or ""):
        raise ValueError("invalid backup name: %r" % name)
    root = os.path.realpath(_backups_root())
    path = os.path.realpath(os.path.join(root, name))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("backup path escapes store: %r" % name)
    return path


def _infer_db_engine(base: str) -> Optional[str]:
    env = instance._read_app_env(base)
    url = (env.get("DB_URL") or "").lower()
    for key in ("postgresql", "mariadb", "mysql", "mongodb"):
        if key in url:
            return key
    return None


def _parse_xmx_mb(java_opts: str) -> Optional[int]:
    m = re.search(r"-Xmx(\d+)([mg])", java_opts or "", re.I)
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2).lower() == "g" else n


def _build_manifest(app: str, base: str) -> Dict:
    info = instance._app_info(app)
    setenv = instance._read_setenv(base)
    app_env = instance._read_app_env(base)
    return {
        "format": MANIFEST_FORMAT,
        "app": app,
        "type": info.get("type") or "war",
        "tomcat_major": info.get("tomcat"),
        "java_major": info.get("java"),
        "memory_mb": _parse_xmx_mb(setenv.get("JAVA_OPTS", "")),
        # JAR services do not have setenv.sh. New backups persist their validated
        # flags in the manifest so a restore never silently drops them.
        "java_opts": app_env.get("JAVA_OPTS", "") if info.get("type") == "jar" else None,
        "port": info.get("port"),
        "domain": info.get("domain"),
        "ssl_enabled": bool(info.get("ssl")),
        "db_engine": _infer_db_engine(base),
        "created_at": _now_iso(),
        "plugin_version": _plugin_version(),
    }


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def backup_app(app: str, remotes=None) -> Dict:
    """Create a local archive of <app> and optionally fan it out to storage profiles.

    `remotes`: None/[] → local only; "all" → every enabled profile; a csv string or
    list of profile ids → those. Returns {app, archive, name, size_*, locations,
    uploaded_to, upload_results} — partial remote failure still keeps the local copy
    and is reported per destination (never silently dropped)."""
    app = validate.identifier(app, "app")
    base = instance.base_path(app)
    if not instance.exists(app):
        raise RuntimeError("no such app: %s" % app)
    if remotes is True:          # legacy bool → all enabled
        remotes = "all"

    root = _backups_root()
    fs.ensure_dir(root)
    fs.mark_managed(root)  # so safe_rmtree/delete can operate here

    manifest = _build_manifest(app, base)
    staging = fs.mkdtemp("jh-backup-")
    try:
        man_path = os.path.join(staging, MANIFEST_NAME)
        fs.atomic_write(man_path, json.dumps(manifest, indent=2) + "\n", mode=0o600)

        members = [(man_path, MANIFEST_NAME)]
        for sub in ("conf", "webapps", "bin"):
            p = os.path.join(base, sub)
            if os.path.isdir(p):
                members.append((p, "base/%s" % sub))
        jar = os.path.join(base, "app.jar")
        if os.path.isfile(jar):
            members.append((jar, "base/app.jar"))
        vhost = proxy.vhost_path(app)
        if os.path.isfile(vhost):
            members.append((vhost, "nginx/%s.conf" % app))

        name = "backup-%s-%s.tar.gz" % (app, _now_stamp())
        dest = os.path.join(root, name)
        archive.pack(members, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    size = os.path.getsize(dest)
    out = {"app": app, "archive": dest, "name": name,
           "size_bytes": size, "size_mb": round(size / (1 << 20), 2),
           "locations": ["local"], "uploaded_to": [], "upload_results": {}}

    # fan out to selected destinations (best-effort, per-destination result)
    ids = []
    if remotes not in (None, "", []):
        try:
            from . import remote as remotemod
            ids = remotemod._resolve_ids(remotes)
            if ids:
                up = remotemod.upload(dest, name, ids)
                out["upload_results"] = up.get("results", {})
                out["uploaded_to"] = up.get("ok_ids", [])
                out["locations"] += up.get("ok_ids", [])
        except Exception as e:
            out["upload_results"] = {"_error": {"ok": False, "detail": str(e)}}

    # sidecar manifest (cheap listing) — records where it was uploaded
    try:
        side = dict(manifest)
        side["name"] = name
        side["uploaded_to"] = out["uploaded_to"]
        fs.atomic_write(_sidecar(dest), json.dumps(side, indent=2) + "\n", mode=0o600)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# listing / deletion / retention
# --------------------------------------------------------------------------- #
def _read_manifest_file(path: str) -> Dict:
    """Manifest for a local archive — read the cheap sidecar `<archive>.json` first,
    falling back to opening the gzip tarball (older backups), and lazily writing the
    sidecar so the next listing is fast."""
    side = _sidecar(path)
    try:
        if os.path.isfile(side):
            return json.loads(open(side, errors="replace").read())
    except Exception:
        pass
    raw = archive.read_member_bytes(path, MANIFEST_NAME)
    if not raw:
        return {}
    try:
        man = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}
    try:  # backfill the sidecar for next time
        m = dict(man)
        m["name"] = os.path.basename(path)
        fs.atomic_write(side, json.dumps(m, indent=2) + "\n", mode=0o600)
    except Exception:
        pass
    return man


def list_backups(app: Optional[str] = None, include_remote: bool = False) -> List[Dict]:
    """Newest-first backup records. Each carries `locations` — the union of the local
    store + (when include_remote) every enabled storage profile that holds it."""
    out: List[Dict] = []
    by_name: Dict[str, Dict] = {}
    root = _backups_root()
    if os.path.isdir(root):
        for name in os.listdir(root):
            if not _NAME_RE.match(name):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            man = _read_manifest_file(path)
            entry_app = man.get("app") or _app_from_name(name)
            if app and entry_app != app:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            rec = {
                "name": name, "app": entry_app, "type": man.get("type"),
                "domain": man.get("domain"), "ssl_enabled": man.get("ssl_enabled"),
                "created_at": man.get("created_at"), "size_bytes": size,
                "size_mb": round(size / (1 << 20), 2), "locations": ["local"],
            }
            out.append(rec)
            by_name[name] = rec
    if include_remote:
        try:
            from . import remote
            for pid in remote.enabled_ids():
                for r in remote.list_remote(pid):
                    if app and r.get("app") != app:
                        continue
                    rec = by_name.get(r["name"])
                    if rec:
                        rec["locations"].append(pid)
                    else:
                        rec = {"name": r["name"], "app": r.get("app"), "type": None,
                               "domain": None, "ssl_enabled": None, "created_at": None,
                               "size_bytes": r.get("size_bytes", 0),
                               "size_mb": r.get("size_mb", 0), "locations": [pid]}
                        out.append(rec)
                        by_name[r["name"]] = rec
        except Exception:
            pass
    out.sort(key=lambda b: (b.get("created_at") or b.get("name") or ""), reverse=True)
    return out


def ensure_local(name: str, profile: Optional[str] = None) -> str:
    """Return a local path for backup <name>, downloading it from storage (the named
    profile, or any enabled profile that has it) when absent. Raises if unavailable."""
    path = _backup_path(name)
    if os.path.isfile(path):
        return path
    fs.ensure_dir(_backups_root())
    from . import remote
    if remote.configured():
        res = remote.download(name, path, profile)
        if res.get("ok") and os.path.isfile(path):
            return path
        raise RuntimeError("remote download failed: %s" % res.get("detail"))
    raise FileNotFoundError("backup not found locally and no storage profile configured: %s" % name)


def delete_backup(name: str, locations=None) -> Dict:
    """Delete a backup. `locations` None → everywhere it exists (local + all enabled
    profiles); else a csv/list selecting `local` and/or specific profile ids."""
    path = _backup_path(name)  # validates name + containment
    if isinstance(locations, str):
        locations = [x.strip() for x in locations.split(",") if x.strip()]
    want_local = locations is None or "local" in locations
    removed = False
    if want_local and os.path.isfile(path):
        os.unlink(path)
        removed = True
        try:
            if os.path.isfile(_sidecar(path)):
                os.unlink(_sidecar(path))
        except OSError:
            pass
    removed_from = []
    try:
        from . import remote
        if remote.configured():
            rids = None if locations is None else [l for l in locations if l != "local"]
            if locations is None or rids:
                removed_from = remote.delete(name, rids).get("removed_from", [])
    except Exception:
        pass
    return {"name": name, "removed": removed, "removed_from": removed_from}


def prune_backups(app: str, keep: int) -> Dict:
    """Keep the newest `keep` backups for <app> at EACH destination — local and every
    enabled storage profile, independently. Names embed a sortable UTC timestamp."""
    app = validate.identifier(app, "app")
    keep = max(0, int(keep))
    # local
    local_names = sorted(
        (b["name"] for b in list_backups(app=app) if "local" in b.get("locations", [])),
        reverse=True)
    removed: List[str] = []
    for n in local_names[keep:]:
        try:
            delete_backup(n, locations=["local"])
            removed.append(n)
        except Exception:
            pass
    # each remote profile, independently
    remote_removed: Dict[str, List[str]] = {}
    try:
        from . import remote
        for pid in remote.enabled_ids():
            names = sorted((r["name"] for r in remote.list_remote(pid) if r.get("app") == app),
                           reverse=True)
            dropped = []
            for n in names[keep:]:
                if pid in remote.delete(n, [pid]).get("removed_from", []):
                    dropped.append(n)
            if dropped:
                remote_removed[pid] = dropped
    except Exception:
        pass
    return {"app": app, "kept": min(keep, len(local_names)),
            "removed": removed, "remote_removed": remote_removed}


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def _rewrite_port(base: str, itype: str, new_port: int) -> None:
    """Point a restored instance at a freshly-allocated port."""
    if itype == "jar":
        envp = os.path.join(base, "bin", "app.env")
        if os.path.isfile(envp):
            body = open(envp, errors="replace").read()
            if re.search(r"^SERVER_PORT=\d+", body, re.M):
                body = re.sub(r"^SERVER_PORT=\d+", "SERVER_PORT=%d" % new_port, body, flags=re.M)
            else:
                body = "SERVER_PORT=%d\n" % new_port + body
            fs.atomic_write(envp, body, mode=0o640)
    else:
        sx = os.path.join(base, "conf", "server.xml")
        if os.path.isfile(sx):
            body = open(sx, errors="replace").read()
            body = re.sub(r'(Connector\s+port=")\d+(")', r"\g<1>%d\g<2>" % new_port, body, count=1)
            fs.atomic_write(sx, body, mode=0o640)


def _clear_site_markers(base: str) -> None:
    for n in ("site.domain", "site.ssl"):
        p = os.path.join(base, "bin", n)
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass


def _safe_opts(raw, java_major: int) -> List[str]:
    """Parse archived JVM flags without ever accepting shell syntax."""
    if isinstance(raw, list):
        opts = [str(v) for v in raw]
    else:
        try:
            opts = shlex.split(str(raw or ""), posix=True)
        except ValueError as e:
            raise ValueError("invalid archived JAVA_OPTS: %s" % e)
    cleaned, warnings = jvm_opts.sanitize(opts, java_major)
    if warnings or cleaned != opts:
        raise ValueError("unsafe or unsupported archived JAVA_OPTS")
    return cleaned


def _validate_restore_payload(src_base: str, manifest: Dict) -> Dict:
    """Treat every archive field and config file as untrusted input."""
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ValueError("unsupported backup manifest format: %r" % manifest.get("format"))
    itype = str(manifest.get("type") or "")
    if itype not in ("war", "jar"):
        raise ValueError("unsupported restored app type: %r" % itype)
    actual = instance._instance_type(src_base)
    if (itype == "jar" and actual != "jar") or (itype == "war" and actual == "jar"):
        raise ValueError("manifest app type does not match archive payload")

    config_port = instance._read_port(src_base)
    port = validate.port(config_port if config_port is not None else manifest.get("port"))
    if manifest.get("port") is not None and validate.port(manifest["port"]) != port:
        raise ValueError("manifest port does not match archive payload")

    setenv_path = os.path.join(src_base, "bin", "setenv.sh")
    if os.path.isfile(setenv_path):
        with open(setenv_path, errors="strict") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not re.fullmatch(r'export\s+[A-Z][A-Z0-9_]*="[^"\r\n]*"', stripped):
                    raise ValueError("invalid archived setenv configuration")
    env = instance._read_setenv(src_base)
    app_env = instance._read_app_env(src_base)
    java_home = env.get("JAVA_HOME") or app_env.get("JAVA_HOME", "")
    detected = {major: os.path.realpath(path) for major, path in java.detect().items()}
    java_real = os.path.realpath(java_home) if java_home else ""
    matching = [major for major, path in detected.items() if path == java_real]
    if not matching:
        raise ValueError("archived JAVA_HOME is not an installed managed runtime")
    java_major = matching[0]
    if manifest.get("java_major") is not None:
        expected_java = validate.java_major(manifest["java_major"])
        if expected_java != java_major:
            raise ValueError("manifest Java version does not match JAVA_HOME")

    if itype == "jar":
        if not os.path.isfile(os.path.join(src_base, "app.jar")):
            raise ValueError("JAR archive is missing app.jar")
        raw_opts = manifest.get("java_opts", app_env.get("JAVA_OPTS", ""))
        catalina_home = ""
    else:
        major = validate.tomcat_version(manifest.get("tomcat_major"))
        catalina_home = env.get("CATALINA_HOME", "")
        expected_home = os.path.realpath(installer.home_path(major))
        if not catalina_home or os.path.realpath(catalina_home) != expected_home:
            raise ValueError("archived CATALINA_HOME is not the managed Tomcat runtime")
        if not installer.is_installed(major):
            raise ValueError("required managed Tomcat runtime is not installed")
        raw_opts = env.get("JAVA_OPTS", "")
    opts = _safe_opts(raw_opts, java_major)
    return {"type": itype, "port": port, "java_home": java_real,
            "java_major": java_major, "catalina_home": catalina_home,
            "opts": opts}


def _unit_path(app: str, backend: Optional[str]) -> Optional[str]:
    if backend == "systemd":
        return service._unit_path(app)
    if backend == "initd":
        return service._script_path(app)
    return None


def _service_snapshot(app: str) -> Dict:
    backend = service._backend(app)
    path = _unit_path(app, backend)
    status = service.status(app)
    snap = {"backend": backend, "path": path, "content": None, "mode": None,
            "active": status == "active", "status": status,
            "enabled": instance._is_enabled(app, backend), "user": "www"}
    if path and os.path.isfile(path) and not os.path.islink(path):
        with open(path, errors="strict") as f:
            snap["content"] = f.read()
        snap["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        m = re.search(r"^(?:User|RUNAS)=\"?([A-Za-z0-9._-]+)\"?$", snap["content"], re.M)
        if m:
            snap["user"] = validate.identifier(m.group(1), "service user")
    return snap


def _install_payload_service(app: str, base: str, cfg: Dict, user: str,
                             backend: Optional[str] = None) -> str:
    """Render for the prior backend when overwriting; auto-select for new apps."""
    if backend is None:
        if cfg["type"] == "jar":
            return service.install_jar_unit(app, cfg["java_home"], base, cfg["port"],
                                            java_opts=" ".join(cfg["opts"]), user=user)
        service.write_setenv(base, app, cfg["java_home"], cfg["catalina_home"], cfg["opts"], [])
        return service.install_unit(app, cfg["java_home"], cfg["catalina_home"], base, user=user)

    if cfg["type"] == "jar":
        ctx = {"app": app, "user": user, "group": user, "java_home": cfg["java_home"],
               "app_dir": base, "port": str(cfg["port"]),
               "java_opts": " ".join(cfg["opts"])}
        template = "%s-jar.%s.tmpl" % ("systemd" if backend == "systemd" else "initd",
                                         "service" if backend == "systemd" else "sh")
    else:
        service.write_setenv(base, app, cfg["java_home"], cfg["catalina_home"], cfg["opts"], [])
        ctx = service._ctx(app, cfg["java_home"], cfg["catalina_home"], base, user)
        template = "systemd.service.tmpl" if backend == "systemd" else "initd.sh.tmpl"
    path = _unit_path(app, backend)
    service._write_unit_file(path, templating.render_file(template, ctx),
                             0o644 if backend == "systemd" else 0o755)
    if backend == "systemd":
        shell.run(["systemctl", "daemon-reload"])
    return path


def _set_enabled(app: str, backend: Optional[str], enabled: Optional[bool]) -> None:
    if enabled is None or backend is None:
        return
    if backend == "systemd":
        shell.run(["systemctl", "enable" if enabled else "disable",
                   "javahost-%s.service" % app])
        return
    tool = shell.which("update-rc.d")
    if not tool:
        raise RuntimeError("cannot restore init.d enabled state: update-rc.d unavailable")
    shell.run([tool, "javahost-%s" % app, "enable" if enabled else "disable"])


def _apply_service_state(app: str, snap: Dict) -> None:
    _set_enabled(app, snap.get("backend"), snap.get("enabled"))
    if snap.get("active"):
        service.action(app, "start")
        if service.status(app) != "active":
            raise RuntimeError("restored service did not become active")


def _restore_unit_snapshot(app: str, snap: Dict) -> None:
    """Restore exact pre-transaction unit bytes/mode, or remove a new unit."""
    current = service._backend(app)
    if snap.get("content") is None:
        if current:
            service.remove_unit(app)
        return
    if current and current != snap["backend"]:
        service.remove_unit(app)
    service._write_unit_file(snap["path"], snap["content"], snap["mode"])
    if snap["backend"] == "systemd":
        shell.run(["systemctl", "daemon-reload"])


def restore(archive_path: str, as_name: Optional[str] = None,
            domain: Optional[str] = None, user: str = "www") -> Dict:
    """Restore an app from a backup archive.

    archive_path : a real .tar.gz (resolved by the endpoint; may be in the store
                   or an uploaded staging path).
    as_name      : None -> overwrite the original app in place (original port +
                   domain). Set -> create a NEW app (reallocated port; domain only
                   if `domain` is given, else the site is dropped).
    """
    if not archive_path or not os.path.isfile(archive_path):
        raise FileNotFoundError("archive not found: %r" % archive_path)
    raw_manifest = archive.read_member_bytes(archive_path, MANIFEST_NAME)
    try:
        manifest = json.loads(raw_manifest.decode("utf-8", "strict")) if raw_manifest else None
    except (UnicodeDecodeError, ValueError):
        manifest = None
    if not isinstance(manifest, dict) or not manifest:
        raise RuntimeError("archive has no manifest.json (not a JavaHost backup)")
    src_app = manifest.get("app")
    new_mode = as_name is not None
    target = validate.identifier(as_name or src_app, "app")
    user = validate.identifier(user, "service user")
    if domain:
        domain = validate.domain(domain)

    base = instance.base_path(target)
    cleanup_warning = None
    with _app_restore_lock(target):
        if os.path.islink(base):
            raise RuntimeError("refusing to restore over symlink target: %s" % target)
        if os.path.lexists(base) and not os.path.isdir(base):
            raise RuntimeError("restore target is not a directory: %s" % target)
        had_existing = os.path.isdir(base)
        if had_existing and not fs.is_managed(base):
            raise RuntimeError("refusing to restore over unmanaged app: %s" % target)
        if new_mode and had_existing:
            raise RuntimeError("app already exists: %s (choose another name)" % target)

        root = _restore_root()
        txn = tempfile.mkdtemp(prefix="%s-" % target, dir=root)
        os.chmod(txn, 0o700)
        fs.mark_managed(txn)
        staging = os.path.join(txn, "staging")
        os.mkdir(staging, 0o700)
        incoming = os.path.join(txn, "incoming")
        recovery = os.path.join(txn, "recovery") if had_existing else None
        failed_replacement = os.path.join(txn, "failed-replacement")
        snap = None
        replacement_installed = False
        service_install_attempted = False
        committed = False
        retain_txn = False
        try:
            archive.safe_extract_tar(archive_path, staging)
            src_base = os.path.join(staging, "base")
            if not os.path.isdir(src_base):
                raise RuntimeError("archive missing base/ payload")
            cfg = _validate_restore_payload(src_base, manifest)
            if new_mode:
                cfg["port"] = instance.allocate_port()
                _rewrite_port(src_base, cfg["type"], cfg["port"])
                _clear_site_markers(src_base)
                dom = domain
            else:
                dom = manifest.get("domain")
                if dom:
                    dom = validate.domain(dom)

            os.replace(src_base, incoming)
            fs.mark_managed(incoming)
            shell.run(["chown", "-R", "%s:%s" % (user, user), incoming], check=False)

            if new_mode and os.path.lexists(base):
                raise RuntimeError("app appeared during restore: %s" % target)
            if had_existing:
                snap = _service_snapshot(target)
                if snap["status"] == "active":
                    service.action(target, "stop")
                    if service.status(target) != "inactive":
                        raise RuntimeError("service did not become inactive; restore aborted")
                elif snap["status"] not in ("inactive", "absent"):
                    raise RuntimeError("cannot prove service is inactive (status: %s)" % snap["status"])
                os.replace(base, recovery)
            os.replace(incoming, base)
            replacement_installed = True

            prior_backend = snap.get("backend") if snap else None
            prior_user = snap.get("user") if snap else user
            service_install_attempted = True
            _install_payload_service(target, base, cfg, prior_user, prior_backend)
            if snap:
                _apply_service_state(target, snap)
            else:
                service.enable_start(target)
            committed = True
        except Exception as restore_error:
            rollback_errors = []
            if replacement_installed and os.path.isdir(base):
                try:
                    if service.status(target) == "active":
                        service.action(target, "stop")
                except Exception as e:
                    rollback_errors.append("stop replacement: %s" % e)
                try:
                    os.replace(base, failed_replacement)
                    replacement_installed = False
                except Exception as e:
                    rollback_errors.append("quarantine replacement: %s" % e)
            if recovery and os.path.isdir(recovery):
                try:
                    if os.path.lexists(base):
                        raise RuntimeError("target path remains occupied")
                    os.replace(recovery, base)
                    recovery = None
                except Exception as e:
                    rollback_errors.append("restore original tree: %s" % e)
            try:
                if snap:
                    _restore_unit_snapshot(target, snap)
                    _apply_service_state(target, snap)
                elif service_install_attempted and service._backend(target):
                    service.remove_unit(target)
            except Exception as e:
                rollback_errors.append("restore service state: %s" % e)
            if rollback_errors:
                retain_txn = True
                raise RuntimeError("restore failed: %s; rollback errors: %s; recovery retained at %s"
                                   % (restore_error, "; ".join(rollback_errors), txn)) from restore_error
            raise
        finally:
            if committed and recovery and os.path.isdir(recovery):
                try:
                    fs.safe_rmtree(recovery, require_marker=True)
                    recovery = None
                except Exception as e:
                    cleanup_warning = "restore committed but recovery cleanup failed: %s" % e
                    retain_txn = True
            if not retain_txn:
                try:
                    fs.safe_rmtree(txn, require_marker=True)
                except Exception as e:
                    if committed:
                        cleanup_warning = "restore committed but transaction cleanup failed: %s" % e
        port = cfg["port"]

    # republish the reverse-proxy site + re-issue SSL (best-effort, never bundle keys)
    ssl_state = False
    ssl_warning = None
    if dom and port:
        try:
            site_res = proxy.set_site(target, dom, int(port))
            if not site_res.get("ok"):
                ssl_warning = "site republish failed: %s" % (
                    site_res.get("error") or "aaPanel registration failed")
            else:
                # Prefer aaPanel-owned path; do not write a parallel plugin vhost.
                pass
        except Exception as e:
            ssl_warning = "site republish failed: %s" % e
        if manifest.get("ssl_enabled") and not ssl_warning:
            try:
                res = ssl.enable(target, dom, port)
                ssl_state = bool(res.get("ssl"))
                if not ssl_state:
                    ssl_warning = "SSL re-issue failed: %s" % res.get("error", "unknown")
            except Exception as e:
                ssl_warning = "SSL re-issue raised: %s" % e

    out = {"app": target, "restored": True, "mode": "new" if new_mode else "overwrite",
           "port": port, "domain": dom, "ssl": ssl_state,
           "status": service.status(target)}
    if ssl_warning:
        out["ssl_warning"] = ssl_warning
    if cleanup_warning:
        out["cleanup_warning"] = cleanup_warning
    return out
