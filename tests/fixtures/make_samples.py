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
              The bytecode target is pinned with `javac --release` (see --release).
  boot.jar    a jar whose Manifest Main-Class is
              org.springframework.boot.loader.JarLauncher plus a BOOT-INF/ marker
              entry, so detect_springboot()==True. Detection-only, not runnable.
  dbcheck.war (optional, --db <engine>): a JSP that reads DB_URL / DB_USER /
              DB_PASSWORD / DB_DRIVER from env, loads the driver, runs `SELECT 1`
              (PG/MySQL/MariaDB via java.sql) and prints
              `DB_OK <engine> <server-version>` or `DB_FAIL:<reason>`. The engine's
              recommended JDBC driver is downloaded from Maven Central into
              WEB-INF/lib/. MongoDB is non-JDBC -> a TCP-connect handshake instead.
  dbapp.jar   (optional, --db <engine>): the "Spring-Boot-shaped DB JAR" stand-in.
              A plain com.sun.net.httpserver app that, on request, opens a JDBC
              connection from the DB_* env and returns `DB_OK <engine>` (or a
              DB_FAIL). Compiled with javac; the recommended driver is bundled in
              a sibling lib/ dir and wired via the manifest Class-Path so
              `java -jar dbapp.jar` finds it. Skipped (warning) if javac absent.
              MongoDB uses the same TCP-handshake probe as dbcheck.war.

BYTECODE TARGET: `--release N` (8|11|17|21) pins app.jar/dbapp.jar via
`javac --release N`, so the matrix can PROVE Java binding (a release-17 jar must
fail to run on Java 8). Defaults to 8 (broadly compatible).

CLI: make_samples.py [--out DIR] [--db postgresql|mysql|mariadb|...]
                     [--release 8|11|17|21] [--all]
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

# Default --release bytecode target: broadly compatible (runs on Java 8+).
DEFAULT_RELEASE = 8
ALLOWED_RELEASES = (8, 11, 17, 21)
# class-file major version per Java release (52=8, 55=11, 61=17, 65=21).
CLASSFILE_MAJOR = {8: 52, 11: 55, 17: 61, 21: 65}

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

# JDBC dbcheck JSP (PG/MySQL/MariaDB). @@DRIVER@@/@@ENGINE@@ are replaced.
# Reads DB_DRIVER from env (falls back to the baked-in class), loads it, runs
# SELECT 1, and prints `DB_OK <engine> <server-version>`.
DBCHECK_JSP_JDBC = """<%@ page contentType="text/plain; charset=UTF-8" import="java.sql.*" %><%
String url = System.getenv("DB_URL");
String user = System.getenv("DB_USER");
String pass = System.getenv("DB_PASSWORD");
String driver = System.getenv("DB_DRIVER");
if (driver == null || driver.isEmpty()) driver = "@@DRIVER@@";
String engine = "@@ENGINE@@";
try {
    Class.forName(driver);
    Connection c = DriverManager.getConnection(url, user, pass);
    String ver = c.getMetaData().getDatabaseProductVersion();
    Statement s = c.createStatement();
    ResultSet rs = s.executeQuery("SELECT 1");
    rs.next();
    int v = rs.getInt(1);
    rs.close(); s.close(); c.close();
    if (v == 1) { out.print("DB_OK " + engine + " " + ver); }
    else { out.print("DB_FAIL:unexpected " + v); }
} catch (Throwable t) {
    out.print("DB_FAIL:" + t.getClass().getSimpleName() + " " + t.getMessage());
}
%>
"""

# MongoDB dbcheck JSP: TCP/handshake probe (non-JDBC). Parses host/port from DB_URL.
DBCHECK_JSP_MONGO = """<%@ page contentType="text/plain; charset=UTF-8" import="java.net.*,java.io.*" %><%
String url = System.getenv("DB_URL");
String engine = "mongodb";
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
    out.print("DB_OK " + engine);
} catch (Throwable t) {
    out.print("DB_FAIL:" + t.getClass().getSimpleName() + " " + t.getMessage());
}
%>
"""

# Runnable DB JAR (PG/MySQL/MariaDB): plain HttpServer that opens a JDBC
# connection from DB_* env on each request and returns `DB_OK <engine>`.
# @@ENGINE@@/@@DRIVER@@ are replaced at generation time; the driver is bundled
# next to the jar (lib/) and wired via the manifest Class-Path.
DBAPP_JAVA_JDBC = """import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpExchange;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

public class DbApp {
    static String check() {
        String url = System.getenv("DB_URL");
        String user = System.getenv("DB_USER");
        String pass = System.getenv("DB_PASSWORD");
        String driver = System.getenv("DB_DRIVER");
        if (driver == null || driver.isEmpty()) driver = "@@DRIVER@@";
        String engine = "@@ENGINE@@";
        try {
            Class.forName(driver);
            Connection c = DriverManager.getConnection(url, user, pass);
            Statement s = c.createStatement();
            ResultSet rs = s.executeQuery("SELECT 1");
            rs.next();
            int v = rs.getInt(1);
            rs.close(); s.close(); c.close();
            return v == 1 ? ("DB_OK " + engine) : ("DB_FAIL:unexpected " + v);
        } catch (Throwable t) {
            return "DB_FAIL:" + t.getClass().getSimpleName() + " " + t.getMessage();
        }
    }
    public static void main(String[] args) throws Exception {
        int port = 8080;
        String p = System.getenv("SERVER_PORT");
        if (p != null && !p.isEmpty()) {
            try { port = Integer.parseInt(p.trim()); } catch (NumberFormatException ignore) {}
        }
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", new com.sun.net.httpserver.HttpHandler() {
            public void handle(HttpExchange ex) throws java.io.IOException {
                byte[] body = check().getBytes(StandardCharsets.UTF_8);
                ex.sendResponseHeaders(200, body.length);
                OutputStream os = ex.getResponseBody();
                os.write(body);
                os.close();
            }
        });
        server.start();
        System.out.println("dbapp listening on " + port);
    }
}
"""

# Runnable DB JAR (MongoDB): TCP/handshake probe (non-JDBC), no driver on the
# classpath needed. Returns `DB_OK mongodb` if the server port accepts a socket.
DBAPP_JAVA_MONGO = """import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpExchange;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class DbApp {
    static String check() {
        String url = System.getenv("DB_URL");
        String engine = "mongodb";
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
            return "DB_OK " + engine;
        } catch (Throwable t) {
            return "DB_FAIL:" + t.getClass().getSimpleName() + " " + t.getMessage();
        }
    }
    public static void main(String[] args) throws Exception {
        int port = 8080;
        String p = System.getenv("SERVER_PORT");
        if (p != null && !p.isEmpty()) {
            try { port = Integer.parseInt(p.trim()); } catch (NumberFormatException ignore) {}
        }
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", new com.sun.net.httpserver.HttpHandler() {
            public void handle(HttpExchange ex) throws java.io.IOException {
                byte[] body = check().getBytes(StandardCharsets.UTF_8);
                ex.sendResponseHeaders(200, body.length);
                OutputStream os = ex.getResponseBody();
                os.write(body);
                os.close();
            }
        });
        server.start();
        System.out.println("dbapp listening on " + port);
    }
}
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


def _fetch_driver(coords: str, dest_dir: str) -> str:
    """Download a Maven driver jar into dest_dir, returning the local path or ""
    (best-effort: a clear warning on failure, e.g. offline)."""
    url = _maven_url(coords)
    local = os.path.join(dest_dir, coords.split(":")[1] + ".jar")
    if _download(url, local):
        return local
    return ""


def _validate_release(release):
    r = int(release)
    if r not in ALLOWED_RELEASES:
        raise ValueError("unsupported --release %r (allowed: %s)"
                         % (release, ", ".join(str(x) for x in ALLOWED_RELEASES)))
    return r


def classfile_major(class_bytes: bytes) -> int:
    """Parse a .class file's bytecode major version (offset 6-7, big-endian).
    52=Java8, 55=Java11, 61=Java17, 65=Java21."""
    if len(class_bytes) < 8 or class_bytes[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("not a Java class file")
    return (class_bytes[6] << 8) | class_bytes[7]


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


def _compile_jar(out: str, jar_name: str, src_name: str, main_class: str,
                 source: str, release: int, classpath_jar: str = "",
                 lib_dir: str = "") -> str:
    """Compile a single Java source with `javac --release <release>` and package
    a runnable jar (Manifest Main-Class + optional Class-Path). If lib_dir/
    classpath_jar are given, the driver jar is copied into out/<lib_dir>/ and a
    relative Class-Path manifest entry points at it (so `java -jar` finds it).
    Returns the jar path; "" (with a warning) if javac is unavailable."""
    if not _have_javac():
        sys.stderr.write("[make_samples] WARN: javac not found; skipping %s\n" % jar_name)
        return ""
    path = os.path.join(out, jar_name)
    tmp = tempfile.mkdtemp(prefix="javahost-jar-")
    try:
        src = os.path.join(tmp, src_name)
        with open(src, "w") as f:
            f.write(source)
        javac = ["javac", "--release", str(release)]
        # the driver only needs to be on the *compile* classpath if the source
        # references driver classes directly; ours uses reflection (Class.forName)
        # + java.sql, so no compile-time classpath is required.
        subprocess.run(javac + ["-d", tmp, src], check=True)
        stem = main_class.split(".")[-1]
        classes = sorted(n for n in os.listdir(tmp)
                         if n.startswith(stem) and n.endswith(".class"))
        # Wire the bundled driver via Class-Path (relative to the jar location).
        class_path_entry = ""
        if classpath_jar and lib_dir:
            dest_lib = os.path.join(out, lib_dir)
            os.makedirs(dest_lib, exist_ok=True)
            bundled = os.path.join(dest_lib, os.path.basename(classpath_jar))
            shutil.copyfile(classpath_jar, bundled)
            class_path_entry = "%s/%s" % (lib_dir, os.path.basename(classpath_jar))
        mf = "Manifest-Version: 1.0\nMain-Class: %s\n" % main_class
        if class_path_entry:
            mf += "Class-Path: %s\n" % class_path_entry
        mf += "\n"
        manifest = os.path.join(tmp, "MANIFEST.MF")
        with open(manifest, "w") as f:
            f.write(mf)
        if shutil.which("jar"):
            cmd = ["jar", "cfm", path, manifest]
            for c in classes:
                cmd += ["-C", tmp, c]
            subprocess.run(cmd, check=True)
        else:
            crlf_mf = mf.replace("\n", "\r\n")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("META-INF/MANIFEST.MF", crlf_mf)
                for c in classes:
                    z.write(os.path.join(tmp, c), c)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return path


def build_app(out: str, release: int = DEFAULT_RELEASE) -> str:
    """Compile App.java with `javac --release` and package a runnable jar.
    Returns path, or "" (with a warning) if javac is unavailable."""
    return _compile_jar(out, "app.jar", "App.java", "App", APP_JAVA, release)


def build_dbapp(out: str, engine_name: str, release: int = DEFAULT_RELEASE) -> str:
    """Build the runnable DB JAR `dbapp.jar` for the given engine. For JDBC
    engines the recommended driver is downloaded into out/lib/ and wired via the
    manifest Class-Path so `java -jar dbapp.jar` resolves it. MongoDB uses the
    TCP-handshake probe (no driver on the classpath). Skipped (warning) if javac
    is unavailable; built WITHOUT a driver (warning) if the download fails."""
    from core.db import engines as dbengines
    engine = dbengines.get(engine_name)
    is_mongo = engine.name == "mongodb"
    if is_mongo:
        source = DBAPP_JAVA_MONGO
        return _compile_jar(out, "dbapp.jar", "DbApp.java", "DbApp", source, release)

    source = (DBAPP_JAVA_JDBC
              .replace("@@DRIVER@@", engine.driver_class)
              .replace("@@ENGINE@@", engine.name))
    driver_jar = ""
    tmp = tempfile.mkdtemp(prefix="javahost-driver-")
    try:
        driver_jar = _fetch_driver(engine.recommend_driver(), tmp)
        if not driver_jar:
            sys.stderr.write("[make_samples] WARN: dbapp.jar built WITHOUT a bundled "
                             "driver (offline?) — runtime will DB_FAIL with ClassNotFound\n")
        return _compile_jar(out, "dbapp.jar", "DbApp.java", "DbApp", source, release,
                            classpath_jar=driver_jar, lib_dir="lib")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        jsp = (DBCHECK_JSP_JDBC
               .replace("@@DRIVER@@", engine.driver_class)
               .replace("@@ENGINE@@", engine.name))

    driver_jar = None
    if not is_mongo:
        tmp = tempfile.mkdtemp(prefix="javahost-driver-")
        driver_jar = _fetch_driver(engine.recommend_driver(), tmp) or None
        if not driver_jar:
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
                    help="also build dbcheck.war + dbapp.jar for this engine "
                         "(postgresql|mysql|mariadb|mongodb)")
    ap.add_argument("--release", type=int, default=DEFAULT_RELEASE,
                    choices=ALLOWED_RELEASES,
                    help="javac bytecode target for app.jar/dbapp.jar "
                         "(8|11|17|21; default 8 = broadly compatible)")
    ap.add_argument("--all", action="store_true",
                    help="build everything (still skips app.jar if javac missing)")
    args = ap.parse_args(argv)

    release = _validate_release(args.release)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    built = []

    built.append(("hello.war", build_hello(out)))
    built.append(("legacy.war", build_legacy(out)))
    built.append(("boot.jar", build_boot(out)))

    app = build_app(out, release)
    if app:
        built.append(("app.jar (release %d)" % release, app))
    else:
        print("SKIPPED app.jar (javac unavailable)")

    if args.db:
        built.append(("dbcheck.war (%s)" % args.db, build_dbcheck(out, args.db)))
        dbapp = build_dbapp(out, args.db, release)
        if dbapp:
            built.append(("dbapp.jar (%s, release %d)" % (args.db, release), dbapp))
        else:
            print("SKIPPED dbapp.jar (javac unavailable)")

    print("Built into %s:" % out)
    for label, path in built:
        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        print("  %-22s %8d bytes  %s" % (label, size, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
