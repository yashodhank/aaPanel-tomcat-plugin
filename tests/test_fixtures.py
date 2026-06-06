# coding: utf-8
"""
Offline pytest for the sample-fixture generator (tests/fixtures/make_samples.py).

No network and no build tool are required for the WAR/boot.jar assertions (they
are pure-zipfile). The app.jar assertion is skipped when javac is unavailable.
"""
import os
import sys
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "plugin", "javahost"))
sys.path.insert(0, os.path.join(_ROOT, "tests", "fixtures"))

import make_samples  # noqa: E402
from core.deploy import war, jar  # noqa: E402


def _names(path):
    with zipfile.ZipFile(path) as z:
        return z.namelist()


def test_hello_war_has_jsp_and_jakarta_webxml(tmp_path):
    path = make_samples.build_hello(str(tmp_path))
    names = _names(path)
    assert "index.jsp" in names
    assert "WEB-INF/web.xml" in names
    with zipfile.ZipFile(path) as z:
        jsp = z.read("index.jsp").decode()
        webxml = z.read("WEB-INF/web.xml").decode()
    assert "JAVAHOST_OK" in jsp
    assert "jakarta.ee/xml/ns/jakartaee" in webxml
    assert "java.sun.com" not in webxml


def test_legacy_war_detects_javax(tmp_path):
    path = make_samples.build_legacy(str(tmp_path))
    assert war.detect_namespace(path) == "javax"
    names = _names(path)
    assert any("javax/servlet/" in n for n in names)
    with zipfile.ZipFile(path) as z:
        webxml = z.read("WEB-INF/web.xml").decode()
    assert "java.sun.com/xml/ns/javaee" in webxml


def test_boot_jar_detects_springboot(tmp_path):
    path = make_samples.build_boot(str(tmp_path))
    assert jar.detect_springboot(path) is True
    assert jar.manifest_main_class(path) == "org.springframework.boot.loader.JarLauncher"
    names = _names(path)
    assert any(n.startswith("BOOT-INF/") for n in names)


def test_app_jar_runnable_when_javac_present(tmp_path):
    if not make_samples._have_javac():
        pytest.skip("javac unavailable; app.jar generation is skipped by design")
    path = make_samples.build_app(str(tmp_path))
    assert path and os.path.isfile(path)
    assert jar.is_executable_jar(path) is True
    assert jar.manifest_main_class(path) == "App"
    # not a Spring Boot jar
    assert jar.detect_springboot(path) is False


def _classfile_major(jar_path, entry):
    with zipfile.ZipFile(jar_path) as z:
        return make_samples.classfile_major(z.read(entry))


def test_release_pins_bytecode_major(tmp_path):
    """--release must change the class-file major version: a release-8 class
    (major 52) must differ from a release-17 class (major 61) — this is what
    lets the matrix PROVE Java binding (a release-17 jar fails on Java 8)."""
    if not make_samples._have_javac():
        pytest.skip("javac unavailable; compiled artifacts are skipped by design")
    os.makedirs(str(tmp_path / "r8"), exist_ok=True)
    os.makedirs(str(tmp_path / "r17"), exist_ok=True)
    j8 = make_samples.build_app(str(tmp_path / "r8"), release=8)
    j17 = make_samples.build_app(str(tmp_path / "r17"), release=17)
    m8 = _classfile_major(j8, "App.class")
    m17 = _classfile_major(j17, "App.class")
    assert m8 == 52, "release 8 should be class major 52, got %d" % m8
    assert m17 == 61, "release 17 should be class major 61, got %d" % m17
    assert m8 != m17


def test_dbapp_jar_has_main_and_driver(tmp_path):
    """dbapp.jar (JDBC engine) must have a Main-Class and wire a bundled driver
    via Class-Path (or bundle it). Driver bundling is best-effort (offline)."""
    if not make_samples._have_javac():
        pytest.skip("javac unavailable; dbapp.jar generation is skipped by design")
    out = str(tmp_path)
    path = make_samples.build_dbapp(out, "postgresql")
    assert path and os.path.isfile(path)
    assert jar.is_executable_jar(path) is True
    assert jar.manifest_main_class(path) == "DbApp"
    # If the driver downloaded, it must be wired via Class-Path and present on disk.
    with zipfile.ZipFile(path) as z:
        mf = z.read("META-INF/MANIFEST.MF").decode()
    lib = os.path.join(out, "lib")
    has_driver = os.path.isdir(lib) and any(n.endswith(".jar") for n in os.listdir(lib))
    if has_driver:
        assert "Class-Path:" in mf
        bundled = [n for n in os.listdir(lib) if n.endswith(".jar")]
        cp_line = "".join(line for line in mf.splitlines() if "Class-Path" in line or line.startswith(" "))
        assert any(n in mf for n in bundled), "bundled driver not referenced in manifest"
    else:
        pytest.skip("driver download unavailable (offline) — Main-Class checks passed")


def test_dbapp_jar_mongo_no_driver(tmp_path):
    """MongoDB dbapp.jar is a non-JDBC TCP probe: Main-Class present, no driver."""
    if not make_samples._have_javac():
        pytest.skip("javac unavailable; dbapp.jar generation is skipped by design")
    path = make_samples.build_dbapp(str(tmp_path), "mongodb")
    assert path and os.path.isfile(path)
    assert jar.manifest_main_class(path) == "DbApp"


def test_dbcheck_war_bundles_driver_when_online(tmp_path):
    """dbcheck.war built with --db must contain a WEB-INF/lib driver jar.
    Skip if the driver download failed (offline)."""
    path = make_samples.build_dbcheck(str(tmp_path), "postgresql")
    names = _names(path)
    lib_jars = [n for n in names if n.startswith("WEB-INF/lib/") and n.endswith(".jar")]
    if not lib_jars:
        pytest.skip("driver download unavailable (offline) — WAR built without driver by design")
    assert any("postgresql" in n for n in lib_jars)
    with zipfile.ZipFile(path) as z:
        jsp = z.read("index.jsp").decode()
    assert "DB_OK" in jsp and "postgresql" in jsp
