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
