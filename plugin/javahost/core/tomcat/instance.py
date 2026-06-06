# coding: utf-8
"""
Per-app Tomcat instance lifecycle (CATALINA_BASE under INSTANCE_ROOT).

Centralizes create / delete / repair / detail / logs so the panel entrypoint
stays thin. Each instance is a lightweight CATALINA_BASE that shares a managed
CATALINA_HOME (see installer.py). All removals are marker-gated (fs.safe_rmtree).
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from . import templating, service, installer, registry
from ..runtime import java, jvm_opts
from ..util import fs, validate

INSTANCE_ROOT = "/www/server/javahost/instances"
_SUBDIRS = ("conf", "webapps", "logs", "work", "temp", "bin")


def base_path(app: str) -> str:
    return os.path.join(INSTANCE_ROOT, validate.identifier(app, "app"))


def exists(app: str) -> bool:
    return os.path.isdir(base_path(app))


def list_apps() -> List[Dict[str, str]]:
    out = []
    if os.path.isdir(INSTANCE_ROOT):
        for name in sorted(os.listdir(INSTANCE_ROOT)):
            if os.path.isdir(os.path.join(INSTANCE_ROOT, name)):
                out.append({"app": name, "status": service.status(name)})
    return out


def _scaffold(base: str) -> None:
    for sub in _SUBDIRS:
        fs.ensure_dir(os.path.join(base, sub))
    fs.mark_managed(base)


def _render_conf(base: str, app: str, port: int) -> None:
    fs.atomic_write(os.path.join(base, "conf", "server.xml"),
                    templating.render_file("server.xml.tmpl", {"http_port": str(port)}),
                    mode=0o640)
    fs.atomic_write(os.path.join(base, "conf", "context.xml"),
                    templating.render_file("context.xml.tmpl", {"app": app}),
                    mode=0o640)


def create(app: str, major: str, port: int, memory_mb: int,
           user: str = "www", prefer_java: Optional[int] = None) -> Dict:
    app = validate.identifier(app, "app")
    major = validate.tomcat_version(major)
    port = validate.port(port)
    memory_mb = validate.memory_mb(memory_mb)
    if not installer.is_installed(major):
        raise RuntimeError("Tomcat %s is not installed" % major)
    if exists(app):
        raise RuntimeError("app already exists: %s" % app)
    home = installer.home_path(major)
    java_home = installer.ensure_java(major, prefer=prefer_java)
    major_java = java.probe(java_home) or registry.get_line(major).min_java
    base = base_path(app)
    _scaffold(base)
    opts, warns = jvm_opts.sanitize(jvm_opts.default_opts(memory_mb), major_java)
    service.write_setenv(base, app, java_home, home, opts, [])
    _render_conf(base, app, port)
    service.install_unit(app, java_home, home, base, user=user)
    service.enable_start(app)
    return {"app": app, "tomcat": major, "port": port, "java": major_java,
            "status": service.status(app), "warnings": warns}


def delete(app: str, *, purge: bool = True) -> Dict:
    app = validate.identifier(app, "app")
    service.remove_unit(app)
    base = base_path(app)
    removed = False
    if purge and os.path.isdir(base):
        fs.safe_rmtree(base, require_marker=True)  # refuses unmanaged dirs
        removed = True
    return {"app": app, "removed": removed}


def repair(app: str) -> Dict:
    """Re-render the service unit from the existing setenv and restart. Useful
    after an OS upgrade or a stale/half-broken unit."""
    app = validate.identifier(app, "app")
    base = base_path(app)
    if not exists(app):
        raise RuntimeError("no such app: %s" % app)
    env = _read_setenv(base)
    java_home = env.get("JAVA_HOME", "")
    home = env.get("CATALINA_HOME", "")
    if not (java_home and home):
        raise RuntimeError("cannot repair %s: setenv missing JAVA_HOME/CATALINA_HOME" % app)
    # clean stale pid, reinstall unit, restart
    pid = os.path.join(base, "temp", "tomcat.pid")
    if os.path.exists(pid):
        try:
            os.unlink(pid)
        except OSError:
            pass
    service.install_unit(app, java_home, home, base)
    service.action(app, "restart") if service.status(app) == "active" else service.enable_start(app)
    return {"app": app, "status": service.status(app), "repaired": True}


def detail(app: str) -> Dict:
    app = validate.identifier(app, "app")
    base = base_path(app)
    if not exists(app):
        raise RuntimeError("no such app: %s" % app)
    env = _read_setenv(base)
    return {
        "app": app,
        "status": service.status(app),
        "port": _read_port(base),
        "java_home": env.get("JAVA_HOME", ""),
        "catalina_home": env.get("CATALINA_HOME", ""),
        "managed": fs.is_managed(base),
        "has_db_env": os.path.isfile(os.path.join(base, "bin", "app.env")),
    }


def tail_log(app: str, lines: int = 200) -> str:
    app = validate.identifier(app, "app")
    base = base_path(app)
    candidates = ["catalina.out"] + [
        f for f in (os.listdir(os.path.join(base, "logs")) if os.path.isdir(os.path.join(base, "logs")) else [])
        if f.startswith("catalina") and f.endswith(".log")
    ]
    for name in candidates:
        path = os.path.join(base, "logs", name)
        if os.path.isfile(path):
            return _tail(path, max(1, min(int(lines), 2000)))
    return ""


# --- helpers ---
def _read_setenv(base: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    path = os.path.join(base, "bin", "setenv.sh")
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                m = re.match(r'\s*export\s+(\w+)="?(.*?)"?\s*$', line)
                if m:
                    env[m.group(1)] = m.group(2)
    return env


def _read_port(base: str) -> Optional[int]:
    sx = os.path.join(base, "conf", "server.xml")
    if os.path.isfile(sx):
        with open(sx, errors="replace") as f:
            m = re.search(r'Connector\s+port="(\d+)"', f.read())
            if m:
                return int(m.group(1))
    return None


def _tail(path: str, lines: int) -> str:
    # memory-safe tail without reading the whole file
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        block, data, found = 4096, b"", 0
        pos = end
        while pos > 0 and found <= lines:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
            found = data.count(b"\n")
        return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", "replace")
