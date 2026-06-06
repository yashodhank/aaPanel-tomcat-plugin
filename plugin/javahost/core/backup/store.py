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
  * overwrite (as_name=None): stop+delete the existing app, restore in place with
    its original port/domain.
  * restore-as-new (as_name set): reallocate the port, rewrite server.xml/app.env,
    remap (or drop) the domain so two apps never collide.

Backups contain DB credentials (bin/app.env), so archives are written 0600 under
the managed backups dir. Names are strictly validated; every path is realpath-
contained. Defensive throughout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Dict, List, Optional

from ..deploy import proxy, ssl
from ..tomcat import instance, service
from ..util import fs, validate
from . import archive

BACKUPS_ROOT = "/www/server/javahost/backups"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1

# backup-<app>-<YYYYmmddTHHMMSSZ>.tar.gz
_NAME_RE = re.compile(r"^backup-[A-Za-z0-9._-]+-\d{8}T\d{6}Z\.tar\.gz$")
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
    """Realpath-contained path for a backup file name under BACKUPS_ROOT."""
    if not _NAME_RE.match(name or ""):
        raise ValueError("invalid backup name: %r" % name)
    root = os.path.realpath(BACKUPS_ROOT)
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
    return {
        "format": MANIFEST_FORMAT,
        "app": app,
        "type": info.get("type") or "war",
        "tomcat_major": info.get("tomcat"),
        "java_major": info.get("java"),
        "memory_mb": _parse_xmx_mb(setenv.get("JAVA_OPTS", "")),
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
def backup_app(app: str, remote: bool = False) -> Dict:
    """Create a local archive of <app>. Returns {app, archive, name, size_bytes,
    size_mb, remote}. `remote` upload is wired in Phase 3 (core/backup/remote)."""
    app = validate.identifier(app, "app")
    base = instance.base_path(app)
    if not instance.exists(app):
        raise RuntimeError("no such app: %s" % app)

    fs.ensure_dir(BACKUPS_ROOT)
    fs.mark_managed(BACKUPS_ROOT)  # so safe_rmtree/delete can operate here

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
        dest = os.path.join(BACKUPS_ROOT, name)
        archive.pack(members, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    size = os.path.getsize(dest)
    out = {"app": app, "archive": dest, "name": name,
           "size_bytes": size, "size_mb": round(size / (1 << 20), 2),
           "remote": False}
    if remote:
        try:
            from . import remote as remotemod  # Phase 3
            up = remotemod.upload(dest, name)
            out["remote"] = bool(up.get("ok"))
            out["remote_detail"] = up.get("detail", "")
        except Exception as e:
            out["remote"] = False
            out["remote_detail"] = "remote upload unavailable: %s" % e
    return out


# --------------------------------------------------------------------------- #
# listing / deletion / retention
# --------------------------------------------------------------------------- #
def _read_manifest_file(path: str) -> Dict:
    raw = archive.read_member_bytes(path, MANIFEST_NAME)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def list_backups(app: Optional[str] = None) -> List[Dict]:
    """Newest-first records for local backups (optionally filtered by app)."""
    out: List[Dict] = []
    if not os.path.isdir(BACKUPS_ROOT):
        return out
    for name in os.listdir(BACKUPS_ROOT):
        if not _NAME_RE.match(name):
            continue
        path = os.path.join(BACKUPS_ROOT, name)
        if not os.path.isfile(path):
            continue
        man = _read_manifest_file(path)
        if app and man.get("app") != app:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        out.append({
            "name": name,
            "app": man.get("app"),
            "type": man.get("type"),
            "domain": man.get("domain"),
            "ssl_enabled": man.get("ssl_enabled"),
            "created_at": man.get("created_at"),
            "size_bytes": size,
            "size_mb": round(size / (1 << 20), 2),
        })
    out.sort(key=lambda b: b.get("created_at") or "", reverse=True)
    return out


def delete_backup(name: str) -> Dict:
    path = _backup_path(name)  # validates name + containment
    removed = False
    if os.path.isfile(path):
        os.unlink(path)
        removed = True
    return {"name": name, "removed": removed}


def prune_backups(app: str, keep: int) -> Dict:
    """Keep the newest `keep` local backups for <app>, delete the rest."""
    app = validate.identifier(app, "app")
    keep = max(0, int(keep))
    backups = list_backups(app=app)  # newest-first
    removed: List[str] = []
    for b in backups[keep:]:
        try:
            delete_backup(b["name"])
            removed.append(b["name"])
        except Exception:
            pass
    return {"app": app, "kept": min(keep, len(backups)), "removed": removed}


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
    manifest = _read_manifest_file(archive_path)
    if not manifest:
        raise RuntimeError("archive has no manifest.json (not a JavaHost backup)")
    src_app = manifest.get("app")
    itype = manifest.get("type") or "war"
    new_mode = as_name is not None
    target = validate.identifier(as_name or src_app, "app")
    if domain:
        domain = validate.domain(domain)

    base = instance.base_path(target)
    if new_mode:
        if instance.exists(target):
            raise RuntimeError("app already exists: %s (choose another name)" % target)
    else:
        # overwrite: stop + remove the existing instance first (marker-gated delete)
        if instance.exists(target):
            try:
                service.action(target, "stop")
            except Exception:
                pass
            service.remove_unit(target)
            instance.delete(target, purge=True)

    # extract to staging, then move base/ into place (hardened extractor)
    staging = fs.mkdtemp("jh-restore-")
    try:
        archive.safe_extract_tar(archive_path, staging)
        src_base = os.path.join(staging, "base")
        if not os.path.isdir(src_base):
            raise RuntimeError("archive missing base/ payload")
        if os.path.isdir(base):
            fs.safe_rmtree(base, require_marker=fs.is_managed(base))
        shutil.move(src_base, base)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    fs.mark_managed(base)

    # port + domain
    if new_mode:
        port = instance.allocate_port()
        _rewrite_port(base, itype, port)
        _clear_site_markers(base)            # never inherit the source domain/cert
        dom = domain                          # only the explicitly-provided one
    else:
        port = instance._read_port(base) or manifest.get("port")
        dom = manifest.get("domain")

    # ownership
    from ..util import shell
    shell.run(["chown", "-R", "%s:%s" % (user, user), base], check=False)

    # re-render setenv (fixes CATALINA_BASE/app for a new name) + the service unit
    env = instance._read_setenv(base)
    java_home = env.get("JAVA_HOME") or instance._read_app_env(base).get("JAVA_HOME", "")
    if itype == "jar":
        service.install_jar_unit(target, java_home, base, port or 0, java_opts="", user=user)
    else:
        catalina_home = env.get("CATALINA_HOME", "")
        opts = [o for o in (env.get("JAVA_OPTS", "") or "").split() if o]
        if java_home and catalina_home:
            service.write_setenv(base, target, java_home, catalina_home, opts, [])
            service.install_unit(target, java_home, catalina_home, base, user=user)
        else:
            raise RuntimeError("restored setenv missing JAVA_HOME/CATALINA_HOME; cannot rebuild unit")

    service.enable_start(target)

    # republish the reverse-proxy site + re-issue SSL (best-effort, never bundle keys)
    ssl_state = False
    ssl_warning = None
    if dom and port:
        try:
            proxy.write_vhost(target, dom, port, ssl=False)
            proxy.ensure_include()
            proxy.reload_nginx()
            proxy._store_domain(target, dom)
        except Exception as e:
            ssl_warning = "site republish failed: %s" % e
        if manifest.get("ssl_enabled"):
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
    return out
