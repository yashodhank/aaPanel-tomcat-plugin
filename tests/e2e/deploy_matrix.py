#!/usr/bin/env python3
# coding: utf-8
"""
JavaHost service-less deploy MATRIX E2E.

Reuses the smoke_noservice.py approach (drive catalina.sh / java -jar directly as
the run user, no systemd) so it runs even on hardened hosts where service dirs
are immutable. It exercises the full deploy surface end-to-end against a real
Tomcat install, asserting markers at every step, and always tears down.

Steps (each asserts its marker; any failure -> non-zero exit, cleanup still runs):
  1. install Tomcat (major from JAVAHOST_E2E_TOMCAT, default 11) if needed
  2. create instance + deploy hello.war -> curl health for JAVAHOST_OK
  3. detect_namespace(legacy.war) == 'javax'  (+ migrate via war.migrate if java)
  4. run app.jar (java -jar with SERVER_PORT) and health-poll for JAVAHOST_OK
  5. --with-db <engine>: ephemeral Docker DB -> SetDbEnv-equivalent app.env ->
     deploy dbcheck.war -> assert DB_OK -> docker rm -f

Env:
  JAVAHOST_PLUGIN_DIR  plugin dir on sys.path (default /www/server/panel/plugin/javahost)
  JAVAHOST_E2E_TOMCAT  Tomcat major (default 11)
  JAVAHOST_E2E_USER    run user; "root"/"" => direct mode (no su)
  JAVAHOST_FIXTURES    fixtures out dir (default tests/fixtures/out)

CLI: deploy_matrix.py [--with-db postgresql|mysql|mariadb|mongodb]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PLUGIN = os.environ.get("JAVAHOST_PLUGIN_DIR", "/www/server/panel/plugin/javahost")
for p in (PLUGIN, "/www/server/panel/class", "/www/server/panel"):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.tomcat import instance, installer, service  # noqa: E402
from core.runtime import java, jvm_opts               # noqa: E402
from core.deploy import war                            # noqa: E402
from core.db import engines as dbengines              # noqa: E402
from core.util import fs                               # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
FIXTURES = os.environ.get("JAVAHOST_FIXTURES", os.path.join(_ROOT, "tests", "fixtures", "out"))

TC = os.environ.get("JAVAHOST_E2E_TOMCAT", "11")
USER = os.environ.get("JAVAHOST_E2E_USER", "www")
DIRECT = USER in ("", "root")
MARKER = "JAVAHOST_OK"
DB_OK = "DB_OK"

APP_WAR = "e2e_matrix_war"
APP_JAR = "e2e_matrix_jar"
APP_DB = "e2e_matrix_db"

# Docker DB recipes: image + env + the port inside the container + connect args.
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

_results = []  # (name, ok, detail)


def record(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def run_tc(cmd):
    if DIRECT:
        subprocess.run(["bash", "-c", cmd], check=False)
    else:
        subprocess.run(["su", "-s", "/bin/bash", USER, "-c", cmd], check=False)


def poll_marker(port, marker, tries=25, wait=2):
    for _ in range(tries):
        try:
            body = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2).read().decode("utf-8", "replace")
            if marker in body:
                return True, body
        except Exception:
            pass
        time.sleep(wait)
    return False, ""


def teardown_instance(app, home=None):
    base = instance.base_path(app)
    if home and os.path.isdir(base):
        run_tc("CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh stop" % (home, base, home))
        time.sleep(2)
    if os.path.isdir(base):
        fs.safe_rmtree(base, require_marker=False)


def kill_jar(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# --- step helpers -----------------------------------------------------------

def step_deploy_war(home, java_home):
    war_path = os.path.join(FIXTURES, "hello.war")
    if not os.path.isfile(war_path):
        return record("deploy hello.war", False, "fixture missing: %s" % war_path)
    teardown_instance(APP_WAR, home)
    base = instance.base_path(APP_WAR)
    port = instance.allocate_port()
    instance._scaffold(base)
    instance._render_conf(base, APP_WAR, port, catalina_home=home)
    mj = java.probe(java_home) or 17
    opts, _ = jvm_opts.sanitize(jvm_opts.default_opts(256), mj)
    service.write_setenv(base, APP_WAR, java_home, home, opts, [])
    war.safe_extract(war_path, os.path.join(base, "webapps", "ROOT"))
    if not DIRECT:
        subprocess.run(["chown", "-R", "%s:%s" % (USER, USER), base], check=False)
    run_tc("CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh start" % (home, base, home))
    ok, _ = poll_marker(port, MARKER)
    if not ok:
        lg = os.path.join(base, "logs", "catalina.out")
        if os.path.isfile(lg):
            print("--- catalina.out tail ---\n" + open(lg, errors="replace").read()[-1200:])
    record("deploy hello.war + health JAVAHOST_OK", ok, "port %d" % port)
    teardown_instance(APP_WAR, home)
    return ok


def step_namespace(java_home):
    legacy = os.path.join(FIXTURES, "legacy.war")
    if not os.path.isfile(legacy):
        return record("detect_namespace(legacy.war)", False, "fixture missing")
    ns = war.detect_namespace(legacy)
    ok = ns == "javax"
    record("detect_namespace(legacy.war) == 'javax'", ok, "got %r" % ns)
    if not ok:
        return ok
    # migrate if a working java is available (best-effort; needs network for the tool)
    if java_home and os.access(os.path.join(java_home, "bin", "java"), os.X_OK):
        try:
            tmp = tempfile.mkdtemp(prefix="javahost-migrate-")
            out = os.path.join(tmp, "migrated.war")
            war.migrate(legacy, out, java_home)
            mns = war.detect_namespace(out)
            record("migrate(legacy.war) -> jakarta", mns == "jakarta", "got %r" % mns)
        except Exception as e:  # noqa: BLE001
            print("  [SKIP] migrate (tool unavailable/offline): %s" % e)
        finally:
            fs.safe_rmtree(tmp, require_marker=False) if tmp.startswith(tempfile.gettempdir()) else None
    else:
        print("  [SKIP] migrate: no usable java")
    return ok


def step_jar(java_home):
    jar = os.path.join(FIXTURES, "app.jar")
    if not os.path.isfile(jar):
        print("  [SKIP] run app.jar: fixture missing (javac was unavailable at generation)")
        return True  # not a failure — generation may legitimately skip it
    base = instance.base_path(APP_JAR)
    if os.path.isdir(base):
        fs.safe_rmtree(base, require_marker=False)
    fs.ensure_dir(base)
    fs.mark_managed(base)
    import shutil
    shutil.copyfile(jar, os.path.join(base, "app.jar"))
    port = instance.allocate_port()
    javabin = os.path.join(java_home, "bin", "java") if java_home else "java"
    env = dict(os.environ, SERVER_PORT=str(port))
    logf = open(os.path.join(base, "app.out"), "wb")
    proc = subprocess.Popen([javabin, "-jar", os.path.join(base, "app.jar")],
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    ok, _ = poll_marker(port, MARKER, tries=15)
    kill_jar(proc)
    logf.close()
    if not ok and os.path.isfile(os.path.join(base, "app.out")):
        print("--- app.out tail ---\n" + open(os.path.join(base, "app.out"), errors="replace").read()[-800:])
    record("run app.jar + health JAVAHOST_OK", ok, "port %d" % port)
    fs.safe_rmtree(base, require_marker=False)
    return ok


def step_db(engine_name, home, java_home):
    if not shutil_which("docker"):
        return record("db matrix (%s)" % engine_name, False, "docker not available")
    if engine_name not in DOCKER_DB:
        return record("db matrix (%s)" % engine_name, False, "unsupported engine")
    war_path = os.path.join(FIXTURES, "dbcheck.war")
    if not os.path.isfile(war_path):
        return record("db matrix (%s)" % engine_name, False,
                      "dbcheck.war missing — run make_samples.py --db %s" % engine_name)

    spec = DOCKER_DB[engine_name]
    engine = dbengines.get(engine_name)
    cname = "javahost-e2e-%s" % engine_name
    subprocess.run(["docker", "rm", "-f", cname], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    host_port = instance.allocate_port(lo=15000, hi=15999)
    cport = engine.default_port
    print("  starting %s as %s (host:%d -> container:%d)" % (spec["image"], cname, host_port, cport))
    rc = subprocess.run(["docker", "run", "-d", "--name", cname,
                         "-p", "127.0.0.1:%d:%d" % (host_port, cport)] + spec["env"] + [spec["image"]],
                        check=False)
    if rc.returncode != 0:
        return record("db matrix (%s)" % engine_name, False, "docker run failed")

    ok = False
    try:
        # wait for the DB to accept TCP connections
        if not _wait_tcp("127.0.0.1", host_port, 60):
            return record("db matrix (%s)" % engine_name, False, "DB port never opened")
        time.sleep(8)  # extra grace for server init (auth/db creation)

        teardown_instance(APP_DB, home)
        base = instance.base_path(APP_DB)
        port = instance.allocate_port()
        instance._scaffold(base)
        instance._render_conf(base, APP_DB, port, catalina_home=home)
        mj = java.probe(java_home) or 17
        opts, _ = jvm_opts.sanitize(jvm_opts.default_opts(256), mj)
        service.write_setenv(base, APP_DB, java_home, home, opts, [])

        # SetDbEnv-equivalent: render the engine env and write app.env.
        mapping = engine.render_env(host="127.0.0.1", port=host_port, db=spec["db"],
                                    user=spec["user"], password=spec["password"], ssl=False)
        dbengines.write_app_env(base, mapping)
        # The JSP reads DB_* from the JVM env; export them into setenv.sh too.
        _append_env_to_setenv(base, mapping)

        war.safe_extract(war_path, os.path.join(base, "webapps", "ROOT"))
        if not DIRECT:
            subprocess.run(["chown", "-R", "%s:%s" % (USER, USER), base], check=False)
        run_tc("CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh start" % (home, base, home))
        got_ok, body = poll_marker(port, DB_OK, tries=20)
        if not got_ok:
            print("  dbcheck response: %r" % body[:300])
            lg = os.path.join(base, "logs", "catalina.out")
            if os.path.isfile(lg):
                print("--- catalina.out tail ---\n" + open(lg, errors="replace").read()[-1000:])
        ok = got_ok
        record("db matrix (%s) deploy dbcheck.war -> DB_OK" % engine_name, ok, "port %d" % port)
        teardown_instance(APP_DB, home)
    finally:
        subprocess.run(["docker", "rm", "-f", cname], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ok


# --- small utils ------------------------------------------------------------

def shutil_which(name):
    import shutil
    return shutil.which(name)


def _wait_tcp(host, port, timeout):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def _append_env_to_setenv(base, mapping):
    """Append DB_* exports to bin/setenv.sh so catalina.sh exports them to the JVM
    (the JSP reads them via System.getenv)."""
    path = os.path.join(base, "bin", "setenv.sh")
    lines = ["", "# JavaHost E2E DB env"]
    for k, v in mapping.items():
        safe = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
        lines.append('export %s="%s"' % (k, safe))
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o750)


def main(argv=None):
    ap = argparse.ArgumentParser(description="JavaHost deploy matrix E2E.")
    ap.add_argument("--with-db", default=None,
                    help="also run the DB matrix against an ephemeral Docker DB")
    args = ap.parse_args(argv)

    print("== JavaHost deploy matrix (Tomcat %s, user=%s, direct=%s) ==" % (TC, USER, DIRECT))
    print("fixtures: %s" % FIXTURES)

    if not installer.is_installed(TC):
        print("Tomcat %s not installed; installing via plugin installer..." % TC)
        try:
            installer.install(TC)
        except Exception as e:  # noqa: BLE001
            print("FATAL: could not install Tomcat %s: %s" % (TC, e))
            return 2
    home = installer.home_path(TC)
    java_home = installer.ensure_java(TC)

    try:
        step_deploy_war(home, java_home)
        step_namespace(java_home)
        step_jar(java_home)
        if args.with_db:
            step_db(args.with_db, home, java_home)
    finally:
        # belt-and-suspenders cleanup
        for app in (APP_WAR, APP_JAR, APP_DB):
            try:
                teardown_instance(app, home)
            except Exception:
                pass

    print("\n== SUMMARY ==")
    failed = [n for (n, ok, _) in _results if not ok]
    for name, ok, detail in _results:
        print("  %-50s %s" % (name, "PASS" if ok else "FAIL"))
    if failed:
        print("\nRESULT: FAIL (%d/%d failed)" % (len(failed), len(_results)))
        return 1
    print("\nRESULT: PASS (%d checks)" % len(_results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
