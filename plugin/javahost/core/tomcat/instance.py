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
from ..util import fs, validate, shell

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


# Full key set every app dict carries, so the UI can render in one round-trip
# without per-app follow-up calls. Order is also the documented return shape.
_APP_KEYS = ("app", "type", "status", "runtime", "tomcat", "java", "port",
             "context", "enabled", "backend", "uptime", "domain", "ssl",
             "runtime_ok")


def _instance_backend(app: str) -> Optional[str]:
    """Which service manager owns this app's unit: 'systemd', 'initd', or None.

    Mirrors service._backend() (which is private) by probing for the installed
    unit/script file — a cheap stat, no subprocess."""
    name = validate.identifier(app, "app")
    if os.path.exists(os.path.join(service.SYSTEMD_DIR, "javahost-%s.service" % name)):
        return "systemd"
    if os.path.exists(os.path.join(service.INITD_DIR, "javahost-%s" % name)):
        return "initd"
    return None


def _is_enabled(app: str, backend: Optional[str]) -> Optional[bool]:
    """Enabled-at-boot state. systemd: a *.wants symlink to the unit; init.d:
    any rc?.d/S* link. Returns None when it can't be cheaply determined.

    The systemd check is a filesystem stat (no `systemctl is-enabled`
    subprocess): an enabled unit has a symlink in some target's `*.wants`
    directory, canonically multi-user.target.wants/javahost-<app>.service."""
    name = validate.identifier(app, "app")
    if backend == "systemd":
        unit = "javahost-%s.service" % name
        try:
            for d in os.listdir(service.SYSTEMD_DIR):
                if not d.endswith(".wants"):
                    continue
                link = os.path.join(service.SYSTEMD_DIR, d, unit)
                if os.path.islink(link) or os.path.exists(link):
                    return True
        except OSError:
            return None
        return False
    if backend == "initd":
        script = "javahost-%s" % name
        try:
            for d in os.listdir("/etc"):
                if not d.startswith("rc") or not d.endswith(".d"):
                    continue
                rcd = os.path.join("/etc", d)
                if os.path.isdir(rcd):
                    for link in os.listdir(rcd):
                        if link.startswith("S") and link.endswith(script):
                            return True
        except OSError:
            return None
        return False
    return None


def _instance_type(base: str) -> str:
    """Classify an instance from its on-disk layout (cheap stats only):
      jar    -> executable/Spring Boot JAR service (app.jar present)
      war    -> Tomcat instance with a deployed app under webapps/
      tomcat -> Tomcat instance with no deployed app yet (empty webapps/)"""
    if os.path.isfile(os.path.join(base, "app.jar")):
        return "jar"
    webapps = os.path.join(base, "webapps")
    try:
        deployed = [e for e in os.listdir(webapps)
                    if not e.startswith(".")] if os.path.isdir(webapps) else []
    except OSError:
        deployed = []
    if deployed:
        return "war"
    return "tomcat"


def _tomcat_major(catalina_home: str) -> Optional[int]:
    """Major Tomcat line from CATALINA_HOME (installer lays it out as
    .../tomcat/<major>). Falls back to the first integer in the path."""
    if not catalina_home:
        return None
    base = os.path.basename(catalina_home.rstrip("/"))
    if base.isdigit():
        return int(base)
    m = re.search(r"(\d+)", catalina_home)
    return int(m.group(1)) if m else None


def _java_major_from_home(java_home: str) -> Optional[int]:
    """Java major parsed cheaply from the JAVA_HOME path (e.g. jdk-21, jdk17,
    jdk8). Avoids spawning `java -version` for every app on every status poll.
    Treats a leading 1.x (legacy 1.8) as major 8."""
    if not java_home:
        return None
    m = re.search(r"(?:jdk|java|jre)[/_-]?(\d+)", java_home, re.I)
    if not m:
        m = re.search(r"(\d+)", os.path.basename(java_home.rstrip("/")))
    if not m:
        return None
    major = int(m.group(1))
    return 8 if major == 1 else major


def _read_context(base: str) -> Optional[str]:
    """Deployed servlet context for a Tomcat instance. ROOT -> '/ROOT', any
    other single webapp -> '/<name>'. None for jar/empty instances."""
    webapps = os.path.join(base, "webapps")
    try:
        entries = sorted(e for e in os.listdir(webapps)
                         if not e.startswith(".")) if os.path.isdir(webapps) else []
    except OSError:
        return None
    # prefer an exploded dir / WAR named ROOT, else the first entry
    names = [os.path.splitext(e)[0] for e in entries]
    if not names:
        return None
    if "ROOT" in names:
        return "/ROOT"
    return "/" + names[0]


def _app_info(name: str) -> Dict:
    """Best-effort rich record for a single instance. Never raises: any field
    that can't be cheaply determined is None, but status is always present."""
    info = {k: None for k in _APP_KEYS}
    info["app"] = name
    try:
        info["status"] = service.status(name)
    except Exception:
        info["status"] = "unknown"
    try:
        base = base_path(name)
        itype = _instance_type(base)
        info["type"] = itype
        env = _read_setenv(base)
        info["port"] = _read_port(base)
        backend = _instance_backend(name)
        info["backend"] = backend
        info["enabled"] = _is_enabled(name, backend)
        if itype == "jar":
            # jar JAVA_HOME lives in bin/app.env (EnvironmentFile), not setenv.sh
            jhome = _read_app_env(base).get("JAVA_HOME") or env.get("JAVA_HOME", "")
            jmaj = _java_major_from_home(jhome)
            info["java"] = jmaj
            info["runtime"] = ("Java %d" % jmaj) if jmaj else None
            # jar apps have no servlet context
        else:
            jhome = env.get("JAVA_HOME", "")
            tmaj = _tomcat_major(env.get("CATALINA_HOME", ""))
            info["tomcat"] = tmaj
            info["java"] = _java_major_from_home(jhome)
            info["runtime"] = ("Tomcat %d" % tmaj) if tmaj else None
            info["context"] = _read_context(base)
        # runtime_ok: the pinned JDK still exists. False here = the app may be
        # "active" on an already-running JVM but its runtime was removed, so it
        # will NOT survive a restart (the UI flags this — status alone lies).
        info["runtime_ok"] = bool(jhome) and os.path.isfile(
            os.path.join(jhome, "bin", "java"))
        # Public reverse-proxy domain, if a site was published (defensive: None).
        try:
            from ..deploy import proxy as _proxy
            info["domain"] = _proxy.read_domain(name)
        except Exception:
            info["domain"] = None
        # Whether SSL has been provisioned for this site (defensive: None on error).
        try:
            from ..deploy import ssl as _ssl
            info["ssl"] = _ssl.read_ssl(name)
        except Exception:
            info["ssl"] = None
        # uptime intentionally left None here: parsing /proc per active app on
        # every 5s status poll is too heavy. The per-app Metrics drawer fetches
        # uptime on demand via GetMetrics. Key is kept (contract) with value None.
        info["uptime"] = None
    except Exception:
        # keep the app listed with whatever we already have (status at minimum)
        pass
    return info


def list_apps() -> List[Dict]:
    """Rich per-app records for the panel — one round-trip, all fields best-effort.

    Each record carries the full _APP_KEYS set; backward-compatible because it
    still includes {app, status}. A single malformed instance dir never breaks
    the list (each app is wrapped in try/except)."""
    out: List[Dict] = []
    if os.path.isdir(INSTANCE_ROOT):
        for name in sorted(os.listdir(INSTANCE_ROOT)):
            if not os.path.isdir(os.path.join(INSTANCE_ROOT, name)):
                continue
            try:
                out.append(_app_info(name))
            except Exception:
                # absolute last resort: list it with a minimal valid record
                rec = {k: None for k in _APP_KEYS}
                rec["app"] = name
                rec["status"] = "unknown"
                out.append(rec)
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
    # Default the JDK to the Tomcat line's baseline (min_java) instead of the
    # newest installed — otherwise every app silently lands on the highest JDK
    # (e.g. 21) regardless of Tomcat. Callers can still pin any JDK via prefer_java.
    if prefer_java in (None, "", 0, "0"):
        prefer_java = registry.get_line(major).min_java
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
               memory_mb: int = 512, user: str = "www", profiles: str = "") -> Dict:
    """Run an executable / Spring Boot fat-JAR as a `java -jar` service.

    `profiles`: optional Spring profiles (SPRING_PROFILES_ACTIVE), e.g. "prod,metrics".
    """
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
    env_lines = [
        "SERVER_PORT=%d" % port,
        # Bind to loopback ONLY: like the Tomcat connector, JAR apps must not face
        # the public interface — they are reached through the reverse proxy. Spring
        # Boot honors SERVER_ADDRESS (server.address); generic apps honor SERVER_HOST.
        "SERVER_ADDRESS=127.0.0.1",
        "SERVER_HOST=127.0.0.1",
        # recorded so list_apps() can report the runtime without spawning java
        "JAVA_HOME=%s" % java_home,
    ]
    if profiles:
        # validate: comma-separated profile identifiers only
        prof = ",".join(p for p in re.split(r"[,\s]+", profiles.strip()) if p)
        if prof and not re.match(r"^[A-Za-z0-9_,-]+$", prof):
            raise ValueError("invalid spring profiles: %r" % profiles)
        if prof:
            env_lines.append("SPRING_PROFILES_ACTIVE=%s" % prof)
    _fs.atomic_write(os.path.join(base, "bin", "app.env"),
                     "\n".join(env_lines) + "\n", mode=0o640)
    shell.run(["chown", "-R", "%s:%s" % (user, user), base], check=False)
    service.install_jar_unit(app, java_home, base, port, java_opts=" ".join(opts), user=user)
    service.enable_start(app)
    return {"app": app, "type": "jar", "port": port, "java": java_major,
            "springboot": jarmod.detect_springboot(jar_src), "profiles": profiles or "",
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


def health_all(timeout: float = 2.0) -> Dict[str, Dict]:
    """Batched health for every managed instance — one round-trip for the UI,
    eliminating the N+1 (one GetHealth per app per poll). Returns
    {app: {"up": bool, "code": int|None, "port": int|None}}.

    Each app's probe is wrapped in try/except: a failing app yields
    {"up": False, "code": None, "port": None}. Never raises."""
    out: Dict[str, Dict] = {}
    if os.path.isdir(INSTANCE_ROOT):
        for name in sorted(os.listdir(INSTANCE_ROOT)):
            if not os.path.isdir(os.path.join(INSTANCE_ROOT, name)):
                continue
            try:
                h = health(name, timeout=timeout)
                out[name] = {"up": h.get("up", False),
                             "code": h.get("code"), "port": h.get("port")}
            except Exception:
                out[name] = {"up": False, "code": None, "port": None}
    return out


def _resolve_pid(app: str) -> Optional[int]:
    """Find the running PID for an app: systemd MainPID, else a pid file."""
    unit = "javahost-%s.service" % validate.identifier(app, "app")
    rc, out, _ = shell.run(["systemctl", "show", "-p", "MainPID", "--value", unit], check=False)
    try:
        pid = int(out.strip())
        if pid > 0:
            return pid
    except (TypeError, ValueError):
        pass
    base = base_path(app)
    for f in (os.path.join(base, "temp", "tomcat.pid"), os.path.join(base, "app.pid")):
        if os.path.isfile(f):
            try:
                pid = int(open(f).read().strip())
                if pid > 0 and os.path.isdir("/proc/%d" % pid):
                    return pid
            except (ValueError, OSError):
                continue
    return None


def metrics(app: str) -> Dict:
    """Lightweight JVM/process metrics from /proc (no psutil dependency)."""
    app = validate.identifier(app, "app")
    pid = _resolve_pid(app)
    out = {"app": app, "pid": pid, "up": pid is not None,
           "rss_mb": None, "threads": None, "uptime_s": None}
    if not pid:
        return out
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = round(int(line.split()[1]) / 1024.0, 1)
                elif line.startswith("Threads:"):
                    out["threads"] = int(line.split()[1])
    except OSError:
        return {**out, "up": False, "pid": None}
    try:  # uptime: system_uptime - process_start_time
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/%d/stat" % pid) as f:
            starttime = int(f.read().split()[21])
        with open("/proc/uptime") as f:
            sys_up = float(f.read().split()[0])
        out["uptime_s"] = int(sys_up - (starttime / hz))
    except (OSError, ValueError, IndexError):
        pass
    return out


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


def _read_app_env(base: str) -> Dict[str, str]:
    """Parse bin/app.env (KEY=val / KEY="val") — the JAR EnvironmentFile."""
    env: Dict[str, str] = {}
    path = os.path.join(base, "bin", "app.env")
    if os.path.isfile(path):
        with open(path, errors="replace") as f:
            for line in f:
                m = re.match(r'\s*(\w+)="?(.*?)"?\s*$', line)
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
