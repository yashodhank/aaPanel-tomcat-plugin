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
import shutil
import socket
import urllib.error
import urllib.request
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


# --- port allocation / conflict detection (closes matrix B5) ---
PORT_LO, PORT_HI = 8080, 8999


def used_ports() -> Dict[int, str]:
    """Ports already claimed by managed instances (from their server.xml)."""
    out: Dict[int, str] = {}
    if os.path.isdir(INSTANCE_ROOT):
        for name in os.listdir(INSTANCE_ROOT):
            p = _read_port(os.path.join(INSTANCE_ROOT, name))
            if p:
                out[p] = name
    return out


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if a process is already listening on host:port (live probe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def allocate_port(preferred: Optional[int] = None, lo: int = PORT_LO, hi: int = PORT_HI) -> int:
    """Pick a free port: honor `preferred` if free, else first unclaimed+unbound
    in [lo, hi]. Raises if `preferred` is taken or no port is available."""
    claimed = used_ports()
    if preferred:
        preferred = validate.port(preferred)
        if preferred in claimed:
            raise RuntimeError("port %d already used by app '%s'" % (preferred, claimed[preferred]))
        if port_in_use(preferred):
            raise RuntimeError("port %d is already in use on this host" % preferred)
        return preferred
    for p in range(lo, hi + 1):
        if p not in claimed and not port_in_use(p):
            return p
    raise RuntimeError("no free port available in range %d-%d" % (lo, hi))


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


def _render_conf(base: str, app: str, port: int, catalina_home: Optional[str] = None) -> None:
    fs.atomic_write(os.path.join(base, "conf", "server.xml"),
                    templating.render_file("server.xml.tmpl", {"http_port": str(port)}),
                    mode=0o640)
    fs.atomic_write(os.path.join(base, "conf", "context.xml"),
                    templating.render_file("context.xml.tmpl", {"app": app}),
                    mode=0o640)
    # A CATALINA_BASE needs the global conf/web.xml (DefaultServlet, welcome-files,
    # mime types). Without it Tomcat logs "No global web.xml found" and serves 404.
    if catalina_home:
        src = os.path.join(catalina_home, "conf", "web.xml")
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(base, "conf", "web.xml"))
            os.chmod(os.path.join(base, "conf", "web.xml"), 0o640)


def create(app: str, major: str, port: int, memory_mb: int,
           user: str = "www", prefer_java: Optional[int] = None) -> Dict:
    app = validate.identifier(app, "app")
    major = validate.tomcat_version(major)
    memory_mb = validate.memory_mb(memory_mb)
    if not installer.is_installed(major):
        raise RuntimeError("Tomcat %s is not installed" % major)
    if exists(app):
        raise RuntimeError("app already exists: %s" % app)
    # allocate_port honors a requested port (and rejects conflicts) or picks a free one
    port = allocate_port(preferred=port if port not in (None, "", 0, "0") else None)
    home = installer.home_path(major)
    java_home = installer.ensure_java(major, prefer=prefer_java)
    major_java = java.probe(java_home) or registry.get_line(major).min_java
    base = base_path(app)
    _scaffold(base)
    opts, warns = jvm_opts.sanitize(jvm_opts.default_opts(memory_mb), major_java)
    service.write_setenv(base, app, java_home, home, opts, [])
    _render_conf(base, app, port, catalina_home=home)
    # The service runs as `user` (default www); give it ownership of CATALINA_BASE
    # so it can write logs/work/temp. Best-effort (needs root; panel runs as root).
    from ..util import shell
    shell.run(["chown", "-R", "%s:%s" % (user, user), base], check=False)
    service.install_unit(app, java_home, home, base, user=user)
    service.enable_start(app)
    return {"app": app, "tomcat": major, "port": port, "java": major_java,
            "status": service.status(app), "warnings": warns}


def create_jar(app: str, jar_src: str, java_major: int, port=None,
               memory_mb: int = 512, user: str = "www") -> Dict:
    """Run an executable / Spring Boot fat-JAR as a `java -jar` service."""
    from ..runtime import java, jvm_opts
    from ..deploy import jar as jarmod
    app = validate.identifier(app, "app")
    java_major = validate.java_major(java_major)
    memory_mb = validate.memory_mb(memory_mb)
    if not jar_src or not os.path.isfile(jar_src):
        raise FileNotFoundError("jar not found: %r" % jar_src)
    if not jarmod.is_executable_jar(jar_src):
        raise RuntimeError("not an executable jar (no Main-Class in MANIFEST): %s" % jar_src)
    if exists(app):
        raise RuntimeError("app already exists: %s" % app)
    java_home = java.resolve(java_major, prefer=java_major) or java.install_temurin(java_major)
    port = allocate_port(preferred=port if port not in (None, "", 0, "0") else None)
    base = base_path(app)
    for sub in ("bin", "logs"):
        fs.ensure_dir(os.path.join(base, sub))
    fs.mark_managed(base)
    shutil.copyfile(jar_src, os.path.join(base, "app.jar"))
    opts, warns = jvm_opts.sanitize(jvm_opts.default_opts(memory_mb), java.probe(java_home) or java_major)
    # app.env carries SERVER_PORT (Spring Boot honors it) — also the port marker for health()
    from ..util import fs as _fs
    _fs.atomic_write(os.path.join(base, "bin", "app.env"),
                     "SERVER_PORT=%d\n" % port, mode=0o640)
    shell.run(["chown", "-R", "%s:%s" % (user, user), base], check=False)
    service.install_jar_unit(app, java_home, base, port, java_opts=" ".join(opts), user=user)
    service.enable_start(app)
    return {"app": app, "type": "jar", "port": port, "java": java_major,
            "springboot": jarmod.detect_springboot(jar_src),
            "status": service.status(app), "warnings": warns}


def health(app: str, timeout: float = 3.0) -> Dict:
    """Probe the app's HTTP port on loopback. Returns {app, up, code, port}."""
    app = validate.identifier(app, "app")
    port = _read_port(base_path(app))
    if not port:
        return {"app": app, "up": False, "code": None, "port": None}
    code = None
    up = False
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=timeout)  # noqa: S310 (loopback)
        code = resp.getcode()
        up = 200 <= code < 500  # any HTTP response means the app is listening
    except urllib.error.HTTPError as e:
        code = e.code
        up = True  # it answered, just not 2xx
    except Exception:
        up = False
    return {"app": app, "up": up, "code": code, "port": port}


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
    # Tomcat instance: port lives in conf/server.xml
    sx = os.path.join(base, "conf", "server.xml")
    if os.path.isfile(sx):
        with open(sx, errors="replace") as f:
            m = re.search(r'Connector\s+port="(\d+)"', f.read())
            if m:
                return int(m.group(1))
    # JAR app: port lives in bin/app.env as SERVER_PORT
    env = os.path.join(base, "bin", "app.env")
    if os.path.isfile(env):
        with open(env, errors="replace") as f:
            m = re.search(r'^SERVER_PORT=(\d+)', f.read(), re.M)
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
