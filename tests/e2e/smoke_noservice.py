#!/usr/bin/env python3
# coding: utf-8
"""
JavaHost service-less E2E smoke — for hosts where the service dirs are locked
(e.g. aaPanel "System Hardening" sets chattr +i on /etc/systemd/system and
/etc/init.d, so install_unit cannot register a service). This harness validates
everything EXCEPT service registration: port allocation, instance scaffold,
server.xml render, WAR deploy, and that Tomcat actually serves the app on the
allocated loopback port — by driving catalina.sh directly as the run user.

Run on a panel host with the panel python. Exit 0 = served OK.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

PLUGIN = os.environ.get("JAVAHOST_PLUGIN_DIR", "/www/server/panel/plugin/javahost")
for p in (PLUGIN, "/www/server/panel/class", "/www/server/panel"):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.tomcat import instance, installer, service  # noqa: E402
from core.runtime import java, jvm_opts               # noqa: E402
from core.deploy import war                            # noqa: E402
from core.util import fs                               # noqa: E402

APP = "e2e_noservice"
TC = os.environ.get("JAVAHOST_E2E_TOMCAT", "11")
USER = os.environ.get("JAVAHOST_E2E_USER", "www")
# Direct mode (run as the invoking user) for hosts that jail `su` (e.g. aaPanel
# security) — set JAVAHOST_E2E_USER=root. Otherwise run the engine as `www`.
DIRECT = USER in ("", "root")
MARKER = "JAVAHOST_E2E_OK"


def run_tc(env_prefix):
    if DIRECT:
        subprocess.run(["bash", "-c", env_prefix], check=False)
    else:
        subprocess.run(["su", "-s", "/bin/bash", USER, "-c", env_prefix], check=False)


def main():
    if not installer.is_installed(TC):
        print("Tomcat %s not installed" % TC); return 2
    home = installer.home_path(TC)
    java_home = installer.ensure_java(TC)
    base = instance.base_path(APP)
    if os.path.isdir(base):
        run_tc("CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh stop" % (home, base, home))
        time.sleep(2)
        fs.safe_rmtree(base, require_marker=False)

    port = instance.allocate_port()
    print("allocated port:", port)
    instance._scaffold(base)
    instance._render_conf(base, APP, port, catalina_home=home)
    mj = java.probe(java_home) or 17
    opts, _ = jvm_opts.sanitize(jvm_opts.default_opts(256), mj)
    service.write_setenv(base, APP, java_home, home, opts, [])

    tmp = tempfile.mkdtemp()
    w = os.path.join(tmp, "app.war")
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("index.html", MARKER)
        z.writestr("WEB-INF/web.xml",
                   '<web-app xmlns="https://jakarta.ee/xml/ns/jakartaee" version="6.0"/>')
    war.safe_extract(w, os.path.join(base, "webapps", "ROOT"))
    if not DIRECT:
        subprocess.run(["chown", "-R", "%s:%s" % (USER, USER), base], check=False)

    run_tc("CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh start" % (home, base, home))
    ok = False
    for _ in range(20):
        try:
            if MARKER in urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2).read().decode():
                ok = True
                break
        except Exception:
            pass
        time.sleep(2)

    print("HEALTH:", "PASS" if ok else "FAIL", "(port %d, as %s)" % (port, USER))
    if not ok:
        lg = os.path.join(base, "logs", "catalina.out")
        if os.path.isfile(lg):
            print("--- catalina.out tail ---")
            print(open(lg, errors="replace").read()[-1500:])

    run_tc("CATALINA_HOME=%s CATALINA_BASE=%s %s/bin/catalina.sh stop" % (home, base, home))
    time.sleep(2)
    fs.safe_rmtree(base, require_marker=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
