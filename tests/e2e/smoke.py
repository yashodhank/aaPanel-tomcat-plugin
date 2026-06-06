#!/usr/bin/env python3
# coding: utf-8
"""
JavaHost end-to-end smoke test — RUN ON A PANEL HOST (root), not in offline CI.

Exercises the real chain: CreateApp (port auto-allocated) -> systemd start ->
deploy a tiny WAR -> restart -> HTTP health-poll the loopback connector ->
GetAppDetail/GetLogs -> DeleteApp cleanup.

Usage (on the box):  /www/server/panel/pyenv/bin/python3 \
    /www/server/panel/plugin/javahost/../../tests/e2e/smoke.py
or simply copy this file over and run it with the panel python.

Exit 0 = healthy, non-zero = failure (prints diagnostics).
"""
import json
import os
import sys
import tempfile
import time
import urllib.request
import zipfile

PLUGIN = os.environ.get("JAVAHOST_PLUGIN_DIR", "/www/server/panel/plugin/javahost")
for p in (PLUGIN, "/www/server/panel/class", "/www/server/panel"):
    if p not in sys.path:
        sys.path.insert(0, p)

from javahost_main import javahost_main  # noqa: E402

APP = "e2e_smoke"
TC = os.environ.get("JAVAHOST_E2E_TOMCAT", "11")
MARKER = "JAVAHOST_E2E_OK"


class G(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def body(r):
    return r.get("msg", r) if isinstance(r, dict) else r


def main():
    m = javahost_main()
    m.DeleteApp(G(app=APP))  # clean slate (ignore result)

    # build a tiny jakarta WAR with a static landing page
    tmp = tempfile.mkdtemp()
    war = os.path.join(tmp, "app.war")
    with zipfile.ZipFile(war, "w") as z:
        z.writestr("index.html", MARKER)
        z.writestr("WEB-INF/web.xml",
                   '<web-app xmlns="https://jakarta.ee/xml/ns/jakartaee" version="6.0"/>')

    r = body(m.CreateApp(G(app=APP, version=TC, port=0, memory=256)))
    print("CreateApp:", json.dumps(r))
    if not isinstance(r, dict) or "port" not in r:
        print("FAIL: CreateApp did not return a port"); return 1
    port = r["port"]

    print("UploadWar:", json.dumps(body(m.UploadWar(G(app=APP, version=TC, tmp=war)))))
    m.AppAction(G(app=APP, action="restart"))

    ok = False
    for _ in range(20):
        try:
            txt = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2).read().decode()
            if MARKER in txt:
                ok = True
                break
        except Exception:
            pass
        time.sleep(2)

    print("DETAIL:", json.dumps(body(m.GetAppDetail(G(app=APP)))))
    if not ok:
        print("LOGS (tail):")
        print(body(m.GetLogs(G(app=APP, lines=40))).get("log", "")[-2000:])
    print("CLEANUP:", json.dumps(body(m.DeleteApp(G(app=APP)))))
    print("HEALTH:", "PASS" if ok else "FAIL", "(port %d)" % port)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
