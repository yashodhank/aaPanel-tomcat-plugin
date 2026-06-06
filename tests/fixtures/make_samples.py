#!/usr/bin/env python3
# coding: utf-8
"""
Generate sample deployment artifacts for the JavaHost deploy matrix / fixtures.

NO build tool (no Maven/Gradle): WARs/JARs are assembled with the stdlib
`zipfile`, and the only compiled artifact (app.jar) uses the JDK's `javac`
directly. If `javac` is unavailable the compiled artifact is skipped with a
clear warning — the rest of the generator still succeeds.

Artifacts (written into --out, default tests/fixtures/out/, which is gitignored):

  hello.war   index.jsp printing the marker `JAVAHOST_OK` + Tomcat/Java version;
              WEB-INF/web.xml on the Jakarta EE 6.0 schema. Tomcat compiles the
              JSP at runtime, so no build tool is needed.
  legacy.war  same JSP but WEB-INF/web.xml on the OLD javax schema
              (http://java.sun.com/xml/ns/javaee) PLUS a WEB-INF/classes/javax/
              servlet/ marker entry, so war.detect_namespace() returns 'javax'
              (exercises MigrateWar).
  app.jar     a single Java source using com.sun.net.httpserver.HttpServer that
              reads SERVER_PORT (default 8080) and responds JAVAHOST_OK; compiled
              with javac and packaged as a runnable jar (Manifest Main-Class).
  boot.jar    a jar whose Manifest Main-Class is
              org.springframework.boot.loader.JarLauncher plus a BOOT-INF/ marker
              entry, so detect_springboot()==True. Detection-only, not runnable.
  dbcheck.war (optional, --db <engine>): a JSP that reads DB_URL / DB_USER /
              DB_PASSWORD from env and runs `SELECT 1` (PG/MySQL/MariaDB via
              java.sql) printing `DB_OK` or `DB_FAIL:<reason>`. The engine's
              recommended JDBC driver is downloaded from Maven Central into
              WEB-INF/lib/. MongoDB is non-JDBC -> a TCP-connect check instead.

CLI: make_samples.py [--out DIR] [--db postgresql|mysql|mariadb|...] [--all]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

# Make the plugin's `core` package importable (for DB driver coords).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_PLUGIN = os.environ.get("JAVAHOST_PLUGIN_DIR", os.path.join(_ROOT, "plugin", "javahost"))
for _p in (_PLUGIN, "/www/server/panel/class", "/www/server/panel"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_OUT = os.path.join(_HERE, "out")
MARKER = "JAVAHOST_OK"
MAVEN_CENTRAL = "https://repo1.maven.org/maven2"

# --- JSP / Java sources -----------------------------------------------------

HELLO_JSP = """<%@ page contentType="text/plain; charset=UTF-8" %><%
out.print("JAVAHOST_OK");
out.print(" tomcat=" + application.getServerInfo());
out.print(" java=" + System.getProperty("java.version"));
%>
"""

# Jakarta EE 6.0 web.xml (Tomcat 10/11).
WEBXML_JAKARTA = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<web-app xmlns="https://jakarta.ee/xml/ns/jakartaee"\n'
    '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
    '         xsi:schemaLocation="https://jakarta.ee/xml/ns/jakartaee '
    'https://jakarta.ee/xml/ns/jakartaee/web-app_6_0.xsd"\n'
    '         version="6.0">\n'
    '  <display-name>javahost-hello</display-name>\n'
    '</web-app>\n'
)

# OLD javax schema (java.sun.com) -> detect_namespace() must report 'javax'.
WEBXML_JAVAX = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<web-app xmlns="http://java.sun.com/xml/ns/javaee"\n'
    '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
    '         xsi:schemaLocation="http://java.sun.com/xml/ns/javaee '
    'http://java.sun.com/xml/ns/javaee/web-app_3_0.xsd"\n'
    '         version="3.0">\n'
    '  <display-name>javahost-legacy</display-name>\n'
    '</web-app>\n'
)

# Plain HttpServer app: reads SERVER_PORT (default 8080), responds JAVAHOST_OK.
APP_JAVA = """import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpExchange;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;

public class App {
    public static void main(String[] args) throws Exception {
        int port = 8080;
        String p = System.getenv("SERVER_PORT");
        if (p != null && !p.isEmpty()) {
            try { port = Integer.parseInt(p.trim()); } catch (NumberFormatException ignore) {}
        }
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", new com.sun.net.httpserver.HttpHandler() {
            public void handle(HttpExchange ex) throws java.io.IOException {
                byte[] body = ("JAVAHOST_OK java=" + System.getProperty("java.version"))
                        .getBytes(StandardCharsets.UTF_8);
                ex.sendResponseHeaders(200, body.length);
                OutputStream os = ex.getResponseBody();
                os.write(body);
                os.close();
            }
        });
        server.start();
        System.out.println("JAVAHOST_OK listening on " + port);
    }
}
"""

# JDBC dbcheck JSP (PG/MySQL/MariaDB). @@DRIVER@@ is replaced with the class.
DBCHECK_JSP_JDBC = """<%@ page contentType="text/plain; charset=UTF-8" import="java.sql.*" %><%
String url = System.getenv("DB_URL");
String user = System.getenv("DB_USER");
String pass = System.getenv("DB_PASSWORD");
try {
    Class.forName("@@DRIVER@@");
    Connection c = DriverManager.getConnection(url, user, pass);
    Statement s = c.createStatement();
    ResultSet rs = s.executeQuery("SELECT 1");
    rs.next();
    int v = rs.getInt(1);
    rs.close(); s.close(); c.close();
    if (v == 1) { out.print("DB_OK"); } else { out.print("DB_FAIL:unexpected " + v); }
} catch (Throwable t) {
    out.print("DB_FAIL:" + t.getClass().getSimpleName() + " " + t.getMessage());
}
%>
"""

# MongoDB dbcheck JSP: TCP-connect probe (non-JDBC). Parses host/port from DB_URL.
DBCHECK_JSP_MONGO = """<%@ page contentType="text/plain; charset=UTF-8" import="java.net.*,java.io.*" %><%
String url = System.getenv("DB_URL");
try {
    String hp = url.replaceFirst("^mongodb://", "");
    int slash = hp.indexOf('/');
    if (slash >= 0) hp = hp.substring(0, slash);
    int q = hp.indexOf('?');
    if (q >= 0) hp = hp.substring(0, q);
    String host = hp; int port = 27017;
    int colon = hp.lastIndexOf(':');
    if (colon >= 0) { host = hp.substring(0, colon); port = Integer.parseInt(hp.substring(colon + 1)); }
    Socket sock = new Socket();
    sock.connect(new InetSocketAddress(host, port), 3000);
    sock.close();
    out.print("DB_OK");
} catch (Throwable t) {
    out.print("DB_FAIL:" + t.getClass().getSimpleName() + " " + t.getMessage());
}
%>
"""


# --- helpers ----------------------------------------------------------------

def _have_javac() -> bool:
    return shutil.which("javac") is not None


def _maven_url(coords: str) -> str:
    """org.postgresql:postgresql:42.7.4 -> Maven Central jar URL."""
    group, artifact, version = coords.split(":")
    return "%s/%s/%s/%s/%s-%s.jar" % (
        MAVEN_CENTRAL, group.replace(".", "/"), artifact, version, artifact, version)


def _download(url: str, dest: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:  # noqa: S310
            shutil.copyfileobj(r, f)
        return True
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[make_samples] WARN: driver download failed (%s): %s\n" % (url, e))
        return False


# --- builders ---------------------------------------------------------------

def build_hello(out: str) -> str:
    path = os.path.join(out, "hello.war")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.jsp", HELLO_JSP)
        z.writestr("WEB-INF/web.xml", WEBXML_JAKARTA)
    return path


def build_legacy(out: str) -> str:
    path = os.path.join(out, "legacy.war")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.jsp", HELLO_JSP)
        z.writestr("WEB-INF/web.xml", WEBXML_JAVAX)
        # marker entry under WEB-INF/classes/javax/servlet/ so detect_namespace
        # sees the javax/servlet/ package path and reports 'javax'.
        z.writestr("WEB-INF/classes/javax/servlet/.javahost-marker",
                   "javax namespace marker\n")
    return path


def build_boot(out: str) -> str:
    """Spring-Boot-shaped jar: Main-Class = JarLauncher + BOOT-INF/ marker.
    Detection-only (not runnable) — assembled with zipfile, no javac."""
    path = os.path.join(out, "boot.jar")
    manifest = (
        "Manifest-Version: 1.0\r\n"
        "Main-Class: org.springframework.boot.loader.JarLauncher\r\n"
        "Start-Class: com.example.DemoApplication\r\n"
        "Spring-Boot-Version: 3.3.0\r\n"
        "\r\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/MANIFEST.MF", manifest)
        z.writestr("BOOT-INF/classes/.javahost-marker", "boot marker\n")
        z.writestr("BOOT-INF/lib/.keep", "")
        z.writestr("org/springframework/boot/loader/JarLauncher.class", "")
    return path


def build_app(out: str) -> str:
    """Compile App.java with javac and package a runnable jar. Returns path,
    or "" (with a warning) if javac is unavailable."""
    if not _have_javac():
        sys.stderr.write("[make_samples] WARN: javac not found; skipping app.jar\n")
        return ""
    path = os.path.join(out, "app.jar")
    tmp = tempfile.mkdtemp(prefix="javahost-appjar-")
    try:
        src = os.path.join(tmp, "App.java")
        with open(src, "w") as f:
            f.write(APP_JAVA)
        subprocess.run(["javac", "-d", tmp, src], check=True)
        # javac emits App.class plus inner classes (e.g. App$1.class for the
        # anonymous HttpHandler) — ALL of them must go into the jar.
        classes = sorted(n for n in os.listdir(tmp)
                         if n.startswith("App") and n.endswith(".class"))
        manifest = os.path.join(tmp, "MANIFEST.MF")
        with open(manifest, "w") as f:
            f.write("Manifest-Version: 1.0\nMain-Class: App\n\n")
        # Use `jar` if present, else assemble the runnable jar with zipfile.
        if shutil.which("jar"):
            cmd = ["jar", "cfm", path, manifest]
            for c in classes:
                cmd += ["-C", tmp, c]
            subprocess.run(cmd, check=True)
        else:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nMain-Class: App\r\n\r\n")
                for c in classes:
                    z.write(os.path.join(tmp, c), c)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return path


def build_dbcheck(out: str, engine_name: str) -> str:
    """Build dbcheck.war for the given engine. For JDBC engines, download the
    recommended driver into WEB-INF/lib/. For MongoDB, TCP-connect probe."""
    from core.db import engines as dbengines
    engine = dbengines.get(engine_name)
    path = os.path.join(out, "dbcheck.war")
    is_mongo = engine.name == "mongodb"
    if is_mongo:
        jsp = DBCHECK_JSP_MONGO
    else:
        jsp = DBCHECK_JSP_JDBC.replace("@@DRIVER@@", engine.driver_class)

    driver_jar = None
    if not is_mongo:
        coords = engine.recommend_driver()  # Maven coords for Java 8+
        url = _maven_url(coords)
        tmp = tempfile.mkdtemp(prefix="javahost-driver-")
        local = os.path.join(tmp, coords.split(":")[1] + ".jar")
        if _download(url, local):
            driver_jar = local
        else:
            sys.stderr.write("[make_samples] WARN: dbcheck.war built WITHOUT a driver "
                             "(offline?) — runtime will DB_FAIL with ClassNotFound\n")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.jsp", jsp)
        z.writestr("WEB-INF/web.xml", WEBXML_JAKARTA)
        if driver_jar:
            z.write(driver_jar, "WEB-INF/lib/" + os.path.basename(driver_jar))
    if not is_mongo and driver_jar:
        shutil.rmtree(os.path.dirname(driver_jar), ignore_errors=True)
    return path


# --- CLI --------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate JavaHost sample deploy artifacts.")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory (default tests/fixtures/out)")
    ap.add_argument("--db", default=None,
                    help="also build dbcheck.war for this engine "
                         "(postgresql|mysql|mariadb|mongodb)")
    ap.add_argument("--all", action="store_true",
                    help="build everything (still skips app.jar if javac missing)")
    args = ap.parse_args(argv)

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    built = []

    built.append(("hello.war", build_hello(out)))
    built.append(("legacy.war", build_legacy(out)))
    built.append(("boot.jar", build_boot(out)))

    app = build_app(out)
    if app:
        built.append(("app.jar", app))
    else:
        print("SKIPPED app.jar (javac unavailable)")

    if args.db:
        built.append(("dbcheck.war (%s)" % args.db, build_dbcheck(out, args.db)))

    print("Built into %s:" % out)
    for label, path in built:
        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        print("  %-22s %8d bytes  %s" % (label, size, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
