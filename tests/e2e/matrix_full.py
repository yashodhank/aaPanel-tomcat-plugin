#!/usr/bin/env python3
# coding: utf-8
"""
JavaHost FULL Tomcat x Java x DB test matrix runner.

Sweeps the *full cartesian* product:

  WAR rows:  Tomcat {9, 10.1, 11} x eligible Java x DB mode {none + each engine}
             eligible Java per line (registry min_java floor):
               Tomcat 9    -> Java 8, 11, 17, 21
               Tomcat 10.1 -> Java 11, 17, 21
               Tomcat 11   -> Java 17, 21
  JAR rows:  executable JAR x Java {8, 11, 17, 21} x DB mode {none + each engine}

DB modes: none | postgresql | mysql | mariadb | mongodb.

Per run it:
  - installs the Tomcat major (plugin installer) if missing,
  - pins the SPECIFIC Java (JAVA_HOME / prefer_java) for that cell,
  - creates a per-app instance,
  - deploys the right artifact:
      none -> hello.war           (marker JAVAHOST_OK)
      db   -> dbcheck.war         (marker DB_OK; JAR rows use dbapp.jar)
      JAR  -> app.jar / dbapp.jar
  - for Tomcat 9 it ALSO deploys+runs legacy.war and asserts it SERVES
    (javax actually runs on T9),
  - health-polls the marker, then tears the run down. ALWAYS cleans up.

Two execution paths, auto-detected & reported:
  systemd     (preferred) — drive via javahost_main endpoints, like smoke.py
              (CreateApp / UploadWar / CreateJarApp / AppAction / GetHealth /
               GetLogs / DeleteApp). Used when the systemd unit dir + instance
               root are writable.
  serviceless (fallback) — drive catalina.sh / java -jar directly as the run
              user, like deploy_matrix.py. Used on hardened hosts.

DB SOURCE (--db-source):
  aapanel  (default) — connect to a locally-installed DB on 127.0.0.1:<default
                       port>. Creds from env JH_<ENGINE>_USER / _PASSWORD / _DB
                       (e.g. JH_POSTGRESQL_USER). Engine env rendered via core.db.
  docker             — ephemeral container per engine (reuses deploy_matrix
                       DOCKER_DB recipes), torn down in finally.

REAL-HOSTNAME assert (--proxy): for a sample of runs, write a JavaHost-owned
Nginx vhost for <app>.5d.bisotech.in (core.deploy.proxy) and assert
`curl http://<app>.5d.bisotech.in/` returns the marker. Skipped gracefully if
nginx is unavailable or the include is not wired.

CLI:
  matrix_full.py [--db-source aapanel|docker] [--proxy]
                 [--only tomcat=11,java=21,db=postgresql,kind=war]
                 [--dry-run]

--dry-run prints the planned matrix (with a count) and exits 0.
Exit non-zero on any failure. NEVER leaves instances/containers behind.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

PLUGIN = os.environ.get("JAVAHOST_PLUGIN_DIR", "/www/server/panel/plugin/javahost")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
# Allow running from a checkout (plugin/javahost) or an installed panel.
for p in (PLUGIN, os.path.join(_ROOT, "plugin", "javahost"),
          "/www/server/panel/class", "/www/server/panel"):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES = os.environ.get("JAVAHOST_FIXTURES", os.path.join(_ROOT, "tests", "fixtures", "out"))

MARKER = "JAVAHOST_OK"
DB_OK = "DB_OK"
DB_MODES = ["none", "postgresql", "mysql", "mariadb", "mongodb"]

# Tomcat major -> eligible Java majors (>= registry min_java).
TOMCAT_JAVA = {
    "9":  [8, 11, 17, 21],
    "10": [11, 17, 21],   # major "10" == line 10.1
    "11": [17, 21],
}
JAR_JAVA = [8, 11, 17, 21]

# Docker DB recipes (mirrors deploy_matrix.py).
DOCKER_DB = {
    "postgresql": {"image": "postgres:17",
                   "env": ["-e", "POSTGRES_PASSWORD=javahost", "-e", "POSTGRES_DB=jhtest"],
                   "db": "jhtest", "user": "postgres", "password": "javahost"},
    "mysql": {"image": "mysql:8.4",
              "env": ["-e", "MYSQL_ROOT_PASSWORD=javahost", "-e", "MYSQL_DATABASE=jhtest"],
              "db": "jhtest", "user": "root", "password": "javahost"},
    "mariadb": {"image": "mariadb:11",
                "env": ["-e", "MARIADB_ROOT_PASSWORD=javahost", "-e", "MARIADB_DATABASE=jhtest"],
                "db": "jhtest", "user": "root", "password": "javahost"},
    "mongodb": {"image": "mongo:8",
                "env": ["-e", "MONGO_INITDB_ROOT_USERNAME=root",
                        "-e", "MONGO_INITDB_ROOT_PASSWORD=javahost"],
                "db": "jhtest", "user": "root", "password": "javahost"},
}

# Proxy: assert the real hostname on a SAMPLE of runs only (loopback stays private).
PROXY_SAMPLE = 2

_results = []   # (cell_id, ok, detail)
_skips = []     # human-readable skip notes (no silent caps)


def log(msg):
    print(msg, flush=True)


def skip(cell_id, why):
    _skips.append("%s — %s" % (cell_id, why))
    log("  [SKIP] %s: %s" % (cell_id, why))


def record(cell_id, ok, detail=""):
    _results.append((cell_id, bool(ok), detail))
    log("  [%s] %s%s" % ("PASS" if ok else "FAIL", cell_id, (" — " + detail) if detail else ""))
    return ok


# --- matrix planning --------------------------------------------------------

def plan_matrix(only):
    """Return the full ordered list of cells. Each cell is a dict:
    {kind: 'war'|'jar', tomcat: <major|None>, java: <int>, db: <mode>, id: str}."""
    cells = []
    for major, javas in TOMCAT_JAVA.items():
        for jv in javas:
            for db in DB_MODES:
                cells.append({"kind": "war", "tomcat": major, "java": jv, "db": db})
    for jv in JAR_JAVA:
        for db in DB_MODES:
            cells.append({"kind": "jar", "tomcat": None, "java": jv, "db": db})
    for c in cells:
        tc = c["tomcat"] or "-"
        c["id"] = "%s/tomcat=%s/java=%d/db=%s" % (c["kind"], tc, c["java"], c["db"])
    if only:
        cells = [c for c in cells if _match_only(c, only)]
    return cells


def _match_only(cell, only):
    for key, val in only.items():
        if key == "tomcat":
            if str(cell["tomcat"]) != str(val):
                return False
        elif key == "java":
            if str(cell["java"]) != str(val):
                return False
        elif key == "db":
            if cell["db"] != val:
                return False
        elif key == "kind":
            if cell["kind"] != val:
                return False
    return True


def parse_only(s):
    if not s:
        return {}
    out = {}
    for part in s.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def print_plan(cells):
    log("== Planned matrix (%d cells) ==" % len(cells))
    war_n = sum(1 for c in cells if c["kind"] == "war")
    jar_n = sum(1 for c in cells if c["kind"] == "jar")
    log("   WAR rows: %d   JAR rows: %d" % (war_n, jar_n))
    for c in cells:
        log("   - " + c["id"])


# --- execution-path detection -----------------------------------------------

def detect_path():
    """Return 'systemd' if the systemd unit dir + instance root are writable,
    else 'serviceless'. Hardened hosts (chattr +i on /etc/systemd) fall back."""
    from core.tomcat import service, instance
    systemd_ok = os.path.isdir(service.SYSTEMD_DIR) and os.access(service.SYSTEMD_DIR, os.W_OK)
    root = instance.INSTANCE_ROOT
    probe = root if os.path.isdir(root) else os.path.dirname(root)
    inst_ok = os.path.isdir(probe) and os.access(probe, os.W_OK)
    have_systemctl = shutil.which("systemctl") is not None
    return "systemd" if (systemd_ok and inst_ok and have_systemctl) else "serviceless"


# --- DB source handling -----------------------------------------------------

def _wait_tcp(host, port, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


class DbHandle(object):
    """Holds a live DB connection target + cleanup. mapping is the rendered
    core.db env (DB_URL/DB_USER/DB_PASSWORD/DB_DRIVER/...)."""
    def __init__(self, mapping, cleanup=None, host_port=None):
        self.mapping = mapping
        self._cleanup = cleanup
        self.host_port = host_port

    def close(self):
        if self._cleanup:
            try:
                self._cleanup()
            except Exception:
                pass


def acquire_db(engine_name, source, cell_id):
    """Bring up / locate a DB for the engine and return a DbHandle, or None
    (with a skip note) if it can't be provided."""
    from core.db import engines as dbengines
    engine = dbengines.get(engine_name)

    if source == "aapanel":
        port = engine.default_port
        if not _wait_tcp("127.0.0.1", port, 3):
            skip(cell_id, "no local %s on 127.0.0.1:%d (aapanel db-source)"
                 % (engine_name, port))
            return None
        env_pfx = "JH_%s" % engine_name.upper()
        user = os.environ.get(env_pfx + "_USER", "root" if engine_name != "postgresql" else "postgres")
        password = os.environ.get(env_pfx + "_PASSWORD", "")
        db = os.environ.get(env_pfx + "_DB", "jhtest")
        mapping = engine.render_env(host="127.0.0.1", port=port, db=db,
                                    user=user, password=password, ssl=False)
        return DbHandle(mapping, cleanup=None, host_port=port)

    # docker
    if not shutil.which("docker"):
        skip(cell_id, "docker not available (docker db-source)")
        return None
    spec = DOCKER_DB[engine_name]
    cname = "javahost-fullmatrix-%s" % engine_name
    subprocess.run(["docker", "rm", "-f", cname], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from core.tomcat import instance
    host_port = instance.allocate_port(lo=15000, hi=15999)
    cport = engine.default_port
    log("  starting %s as %s (127.0.0.1:%d -> %d)" % (spec["image"], cname, host_port, cport))
    rc = subprocess.run(["docker", "run", "-d", "--name", cname,
                         "-p", "127.0.0.1:%d:%d" % (host_port, cport)] + spec["env"] + [spec["image"]],
                        check=False)
    if rc.returncode != 0:
        skip(cell_id, "docker run failed for %s" % engine_name)
        return None

    def _cleanup():
        subprocess.run(["docker", "rm", "-f", cname], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not _wait_tcp("127.0.0.1", host_port, 60):
        _cleanup()
        skip(cell_id, "DB port never opened for %s" % engine_name)
        return None
    time.sleep(8)  # grace for auth/db init
    mapping = engine.render_env(host="127.0.0.1", port=host_port, db=spec["db"],
                               user=spec["user"], password=spec["password"], ssl=False)
    return DbHandle(mapping, cleanup=_cleanup, host_port=host_port)


# --- HTTP poll --------------------------------------------------------------

def poll_marker(url, marker, tries=25, wait=2):
    for _ in range(tries):
        try:
            body = urllib.request.urlopen(url, timeout=2).read().decode("utf-8", "replace")
            if marker in body:
                return True, body
        except Exception:
            pass
        time.sleep(wait)
    return False, ""


# --- artifact selection -----------------------------------------------------

def artifact_for(cell):
    """Return (filename, marker) for the cell's primary artifact."""
    if cell["kind"] == "jar":
        if cell["db"] == "none":
            return "app.jar", MARKER
        return "dbapp.jar", DB_OK
    if cell["db"] == "none":
        return "hello.war", MARKER
    return "dbcheck.war", DB_OK


def ensure_fixture(name, cell_id):
    path = os.path.join(FIXTURES, name)
    if os.path.isfile(path):
        return path
    skip(cell_id, "fixture missing: %s (run make samples-db)" % name)
    return None


_DB_FX_CACHE = {}


def db_fixture(engine, kind, java_release, cell_id):
    """Build (and cache) the engine-specific DB artifact on demand: each engine
    bundles a different JDBC driver, so a single dbcheck.war/dbapp.jar cannot
    serve all engines. Returns the path, or None (skips the cell) on failure."""
    key = (engine, kind, int(java_release))
    if key in _DB_FX_CACHE:
        return _DB_FX_CACHE[key]
    out = os.path.join(FIXTURES, "db_%s_r%d" % (engine, int(java_release)))
    samples = os.path.join(_ROOT, "tests", "fixtures", "make_samples.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = PLUGIN + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run([sys.executable, samples, "--db", engine, "--out", out,
                    "--release", str(int(java_release))], env=env, check=False)
    fname = "dbapp.jar" if kind == "jar" else "dbcheck.war"
    path = os.path.join(out, fname)
    res = path if os.path.isfile(path) else None
    if not res:
        skip(cell_id, "could not build %s fixture for %s (offline/no javac?)" % (fname, engine))
    _DB_FX_CACHE[key] = res
    return res


def resolve_fixture(cell, name):
    """Per-engine DB fixture for DB cells; static fixture otherwise."""
    if cell["db"] != "none":
        return db_fixture(cell["db"], cell["kind"], cell["java"], cell["id"])
    return ensure_fixture(name, cell["id"])


# === systemd path (javahost_main endpoints) =================================

class G(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _body(r):
    return r.get("msg", r) if isinstance(r, dict) else r


def run_cell_systemd(cell, db, proxy_n):
    """Drive a cell via javahost_main endpoints. Returns (ok, did_proxy)."""
    from core.tomcat import instance, installer
    from javahost_main import javahost_main

    m = javahost_main()
    app = ("jh_fm_%s_%s_j%d_%s" % (cell["kind"], cell["tomcat"] or "x",
                                   cell["java"], cell["db"])).replace(".", "")
    name, marker = artifact_for(cell)
    fixture = resolve_fixture(cell, name)
    if not fixture:
        return None, False

    m.DeleteApp(G(app=app))  # clean slate
    did_proxy = False
    ok = False
    try:
        if cell["kind"] == "jar":
            r = _body(m.CreateJarApp(G(app=app, jar=fixture, java=cell["java"],
                                       port=0, memory=256)))
            if not isinstance(r, dict) or "port" not in r:
                return record(cell["id"], False, "CreateJarApp: %r" % r), False
            port = r["port"]
        else:
            # CreateApp does not expose java pinning; call instance.create directly
            # so we can pin the SPECIFIC Java for this cell (prefer_java).
            r = instance.create(app=app, major=cell["tomcat"], port=0,
                                memory_mb=256, prefer_java=cell["java"])
            port = r["port"]
            got_java = r.get("java")
            if got_java and got_java != cell["java"]:
                # Java not present on host; cannot prove this exact cell.
                skip(cell["id"], "Java %d not installed (got %s); pin unsatisfied"
                     % (cell["java"], got_java))
                return None, False
            up = _body(m.UploadWar(G(app=app, version=cell["tomcat"], tmp=fixture)))
            if isinstance(up, dict) and up.get("deployed") is False:
                return record(cell["id"], False, "UploadWar: %r" % up), False

        if cell["db"] != "none":
            m.SetDbEnv(G(app=app, db_engine=cell["db"],
                         db_host="127.0.0.1", db_port=db.host_port,
                         db_name=db.mapping.get("DB_URL", ""),  # informational only
                         db_user=db.mapping.get("DB_USER", ""),
                         db_password=db.mapping.get("DB_PASSWORD", "")))
            # SetDbEnv re-renders; to guarantee the exact rendered env (incl docker
            # host_port + ssl=False) we overwrite app.env with our mapping.
            from core.db import engines as dbengines
            dbengines.write_app_env(instance.base_path(app), db.mapping)

        m.AppAction(G(app=app, action="restart"))
        ok, body = poll_marker("http://127.0.0.1:%d/" % port, marker)
        if not ok:
            logs = _body(m.GetLogs(G(app=app, lines=40)))
            tail = (logs.get("log", "") if isinstance(logs, dict) else str(logs))[-1200:]
            log("--- logs tail (%s) ---\n%s" % (app, tail))
        record(cell["id"], ok, "port %d marker=%s" % (port, marker))

        # Tomcat 9: also prove legacy.war (javax) actually SERVES.
        if ok and cell["kind"] == "war" and cell["tomcat"] == "9" and cell["db"] == "none":
            ok = _t9_legacy_systemd(m, cell)

        # Optional real-hostname proxy assertion on a sample.
        if ok and proxy_n[0] < PROXY_SAMPLE:
            proxy_n[0] += 1
            did_proxy = _proxy_assert(app, port, marker, cell["id"])
    finally:
        m.DeleteApp(G(app=app))
    return ok, did_proxy


def _t9_legacy_systemd(m, parent_cell):
    legacy = ensure_fixture("legacy.war", parent_cell["id"] + "/legacy")
    if not legacy:
        return True  # not a hard failure
    from core.tomcat import instance
    app = "jh_fm_t9_legacy_j%d" % parent_cell["java"]
    m.DeleteApp(G(app=app))
    try:
        r = instance.create(app=app, major="9", port=0, memory_mb=256,
                            prefer_java=parent_cell["java"])
        port = r["port"]
        m.UploadWar(G(app=app, version="9", tmp=legacy))
        m.AppAction(G(app=app, action="restart"))
        ok, _ = poll_marker("http://127.0.0.1:%d/" % port, MARKER)
        record(parent_cell["id"] + " +legacy.war SERVES (javax on T9)", ok, "port %d" % port)
        return ok
    finally:
        m.DeleteApp(G(app=app))


# === serviceless path (catalina.sh / java -jar) ============================

def run_cell_serviceless(cell, db, proxy_n):
    """Drive a cell directly (no systemd). Mirrors deploy_matrix.py patterns."""
    from core.tomcat import instance, installer, service
    from core.runtime import java, jvm_opts
    from core.deploy import war
    from core.db import engines as dbengines
    from core.util import fs

    name, marker = artifact_for(cell)
    fixture = resolve_fixture(cell, name)
    if not fixture:
        return None, False

    # Pin the exact Java for this cell.
    found = java.detect()
    if cell["java"] not in found:
        skip(cell["id"], "Java %d not installed on host" % cell["java"])
        return None, False
    java_home = found[cell["java"]]

    app = ("jh_fm_%s_%s_j%d_%s" % (cell["kind"], cell["tomcat"] or "x",
                                   cell["java"], cell["db"])).replace(".", "")
    base = instance.base_path(app)
    did_proxy = False
    ok = False

    home = None
    if cell["kind"] == "war":
        if not installer.is_installed(cell["tomcat"]):
            try:
                installer.install(cell["tomcat"])
            except Exception as e:  # noqa: BLE001
                skip(cell["id"], "Tomcat %s install failed: %s" % (cell["tomcat"], e))
                return None, False
        home = installer.home_path(cell["tomcat"])

    try:
        if os.path.isdir(base):
            fs.safe_rmtree(base, require_marker=False)

        if cell["kind"] == "jar":
            ok = _serviceless_jar(app, base, fixture, java_home, db, cell, marker)
        else:
            ok = _serviceless_war(app, base, home, java_home, fixture, db, cell, marker)

        if ok and cell["kind"] == "war" and cell["tomcat"] == "9" and cell["db"] == "none":
            ok = _serviceless_t9_legacy(java_home, home, cell)

        if ok and proxy_n[0] < PROXY_SAMPLE and cell.get("_port"):
            proxy_n[0] += 1
            did_proxy = _proxy_assert(app, cell["_port"], marker, cell["id"])
    finally:
        _teardown_serviceless(app, home)
    return ok, did_proxy


def _serviceless_jar(app, base, fixture, java_home, db, cell, marker):
    from core.tomcat import instance
    from core.util import fs
    fs.ensure_dir(base)
    fs.mark_managed(base)
    shutil.copyfile(fixture, os.path.join(base, "app.jar"))
    port = instance.allocate_port()
    cell["_port"] = port
    javabin = os.path.join(java_home, "bin", "java")
    env = dict(os.environ, SERVER_PORT=str(port))
    if db is not None:
        env.update({k: str(v) for k, v in db.mapping.items()})
    logf = open(os.path.join(base, "app.out"), "wb")
    proc = subprocess.Popen([javabin, "-jar", os.path.join(base, "app.jar")],
                            cwd=base, env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        ok, body = poll_marker("http://127.0.0.1:%d/" % port, marker, tries=15)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        logf.close()
    if not ok and os.path.isfile(os.path.join(base, "app.out")):
        log("--- app.out tail ---\n" + open(os.path.join(base, "app.out"),
                                            errors="replace").read()[-1000:])
    return record(cell["id"], ok, "port %d marker=%s" % (port, marker))


def _serviceless_war(app, base, home, java_home, fixture, db, cell, marker):
    from core.tomcat import instance, service
    from core.runtime import java, jvm_opts
    from core.deploy import war
    from core.db import engines as dbengines
    port = instance.allocate_port()
    cell["_port"] = port
    instance._scaffold(base)
    instance._render_conf(base, app, port, catalina_home=home)
    mj = java.probe(java_home) or cell["java"]
    opts, _ = jvm_opts.sanitize(jvm_opts.default_opts(256), mj)
    service.write_setenv(base, app, java_home, home, opts, [])
    if db is not None:
        dbengines.write_app_env(base, db.mapping)
        _append_env_to_setenv(base, db.mapping)
    war.safe_extract(fixture, os.path.join(base, "webapps", "ROOT"))
    _start(home, base)
    ok, body = poll_marker("http://127.0.0.1:%d/" % port, marker, tries=25)
    if not ok:
        lg = os.path.join(base, "logs", "catalina.out")
        if os.path.isfile(lg):
            log("--- catalina.out tail ---\n" + open(lg, errors="replace").read()[-1200:])
    return record(cell["id"], ok, "port %d marker=%s" % (port, marker))


def _serviceless_t9_legacy(java_home, home, parent_cell):
    from core.tomcat import instance, service
    from core.runtime import java, jvm_opts
    from core.deploy import war
    from core.util import fs
    legacy = ensure_fixture("legacy.war", parent_cell["id"] + "/legacy")
    if not legacy:
        return True
    app = "jh_fm_t9_legacy_j%d" % parent_cell["java"]
    base = instance.base_path(app)
    try:
        if os.path.isdir(base):
            fs.safe_rmtree(base, require_marker=False)
        port = instance.allocate_port()
        instance._scaffold(base)
        instance._render_conf(base, app, port, catalina_home=home)
        mj = java.probe(java_home) or parent_cell["java"]
        opts, _ = jvm_opts.sanitize(jvm_opts.default_opts(256), mj)
        service.write_setenv(base, app, java_home, home, opts, [])
        war.safe_extract(legacy, os.path.join(base, "webapps", "ROOT"))
        _start(home, base)
        ok, _ = poll_marker("http://127.0.0.1:%d/" % port, MARKER, tries=25)
        record(parent_cell["id"] + " +legacy.war SERVES (javax on T9)", ok, "port %d" % port)
        return ok
    finally:
        _teardown_serviceless(app, home)


def _start(home, base):
    cmd = "CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh start" % (home, base, home)
    subprocess.run(["bash", "-c", cmd], check=False)


def _teardown_serviceless(app, home):
    from core.tomcat import instance
    from core.util import fs
    base = instance.base_path(app)
    if home and os.path.isdir(base):
        cmd = "CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh stop" % (home, base, home)
        subprocess.run(["bash", "-c", cmd], check=False)
        time.sleep(2)
    if os.path.isdir(base):
        fs.safe_rmtree(base, require_marker=False)


def _append_env_to_setenv(base, mapping):
    path = os.path.join(base, "bin", "setenv.sh")
    lines = ["", "# JavaHost full-matrix DB env"]
    for k, v in mapping.items():
        safe = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
        lines.append('export %s="%s"' % (k, safe))
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o750)


# --- proxy (real hostname) --------------------------------------------------

def _proxy_assert(app, port, marker, cell_id):
    """Write a JavaHost-owned vhost for <app>.5d.bisotech.in and curl it."""
    try:
        from core.deploy import proxy
    except Exception as e:  # noqa: BLE001
        skip(cell_id + " [proxy]", "proxy module unavailable: %s" % e)
        return False
    domain = "%s.%s" % (app.replace("_", "-"), proxy.SITE_SUFFIX)
    try:
        proxy.write_vhost(app, domain, port)
    except Exception as e:  # noqa: BLE001
        skip(cell_id + " [proxy]", "write_vhost failed: %s" % e)
        return False
    try:
        if not proxy.nginx_test():
            skip(cell_id + " [proxy]", "nginx -t failed (include not wired?)")
            proxy.remove_vhost(app)
            return False
        subprocess.run(["bash", "-c", "nginx -s reload || /www/server/nginx/sbin/nginx -s reload"],
                       check=False)
        ok, _ = poll_marker("http://%s/" % domain, marker, tries=8, wait=2)
        record(cell_id + " [proxy %s]" % domain, ok, "real hostname")
        return ok
    finally:
        try:
            proxy.remove_vhost(app)
            subprocess.run(["bash", "-c", "nginx -s reload || /www/server/nginx/sbin/nginx -s reload"],
                           check=False)
        except Exception:
            pass


# --- main -------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="JavaHost FULL Tomcat x Java x DB matrix.")
    ap.add_argument("--db-source", choices=["aapanel", "docker"], default="aapanel",
                    help="DB provider for DB cells (default aapanel = local DB)")
    ap.add_argument("--proxy", action="store_true",
                    help="also assert <app>.5d.bisotech.in via the Nginx vhost on a sample")
    ap.add_argument("--only", default=None,
                    help="filter cells, e.g. tomcat=11,java=21,db=postgresql,kind=war")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned matrix (with a count) and exit")
    args = ap.parse_args(argv)

    only = parse_only(args.only)
    cells = plan_matrix(only)
    print_plan(cells)
    if args.dry_run:
        log("\n(dry-run) %d cells planned; db-source=%s proxy=%s"
            % (len(cells), args.db_source, args.proxy))
        return 0

    path = detect_path()
    log("\n== execution path: %s   db-source: %s   proxy: %s ==\n"
        % (path, args.db_source, args.proxy))
    log("fixtures: %s" % FIXTURES)

    runner = run_cell_systemd if path == "systemd" else run_cell_serviceless
    proxy_n = [0]
    if not args.proxy:
        proxy_n[0] = PROXY_SAMPLE  # disables the sampling

    try:
        for cell in cells:
            log("\n>>> %s" % cell["id"])
            db = None
            if cell["db"] != "none":
                db = acquire_db(cell["db"], args.db_source, cell["id"])
                if db is None:
                    continue  # already recorded as a skip
            try:
                runner(cell, db, proxy_n)
            except Exception as e:  # noqa: BLE001
                record(cell["id"], False, "exception: %s" % e)
            finally:
                if db is not None:
                    db.close()
    finally:
        _final_cleanup()

    return _summary()


def _final_cleanup():
    """Belt-and-suspenders: remove any leftover instances + docker containers."""
    try:
        from core.tomcat import instance
        from core.util import fs
        root = instance.INSTANCE_ROOT
        if os.path.isdir(root):
            for name in os.listdir(root):
                if name.startswith("jh_fm_"):
                    p = os.path.join(root, name)
                    try:
                        fs.safe_rmtree(p, require_marker=False)
                    except Exception:
                        pass
    except Exception:
        pass
    if shutil.which("docker"):
        for engine in DOCKER_DB:
            subprocess.run(["docker", "rm", "-f", "javahost-fullmatrix-%s" % engine],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _summary():
    log("\n== SUMMARY ==")
    failed = [c for (c, ok, _) in _results if not ok]
    for cid, ok, detail in _results:
        log("  %-58s %s" % (cid, "PASS" if ok else "FAIL"))
    if _skips:
        log("\n  SKIPPED (%d):" % len(_skips))
        for s in _skips:
            log("    - " + s)
    log("\n  ran=%d  pass=%d  fail=%d  skipped=%d"
        % (len(_results), len(_results) - len(failed), len(failed), len(_skips)))
    if failed:
        log("RESULT: FAIL")
        return 1
    log("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
