# coding: utf-8
"""Offline unit tests for JavaHost pure-logic modules. No network, no panel."""
import io
import os
import zipfile

import pytest

from core.util import validate
from core.runtime import java, jvm_opts
from core.tomcat import registry, templating
from core.deploy import war


# ---- validate ----
def test_tomcat_version_normalization():
    assert validate.tomcat_version("tomcat10") == "10"
    assert validate.tomcat_version("11.0.22") == "11"
    assert validate.tomcat_version("9") == "9"
    for bad in ("7", "8", "12", "", None, "rm -rf"):
        with pytest.raises(ValueError):
            validate.tomcat_version(bad)


def test_identifier_and_domain_reject_injection():
    assert validate.identifier("my-app_1") == "my-app_1"
    for bad in ("../etc", "a;rm -rf", "a b", "", "."):
        with pytest.raises(ValueError):
            validate.identifier(bad)
    assert validate.domain("App.Example.COM") == "app.example.com"
    for bad in ("-bad.com", "a..b", "x;y", ""):
        with pytest.raises(ValueError):
            validate.domain(bad)


def test_port_and_java_major():
    assert validate.port("8080") == 8080
    with pytest.raises(ValueError):
        validate.port(70000)
    assert validate.java_major("17") == 17
    with pytest.raises(ValueError):
        validate.java_major(13)


# ---- java version parsing ----
@pytest.mark.parametrize("banner,expected", [
    ('openjdk version "1.8.0_402"', 8),
    ('openjdk version "11.0.22" 2024-01-16', 11),
    ('java version "17.0.10" 2024-01-16 LTS', 17),
    ('openjdk version "21.0.2" 2024-01-16', 21),
    ('openjdk 21 2023-09-19', 21),
    ('garbage', None),
])
def test_parse_major(banner, expected):
    assert java.parse_major(banner) == expected


# ---- jvm opts ----
def test_jvm_opts_strip_unsupported_on_17():
    opts = ["-Xmx512m", "-XX:+UseConcMarkSweepGC", "-XX:MaxPermSize=256m", "-XX:+UseG1GC"]
    cleaned, warns = jvm_opts.sanitize(opts, 17)
    assert "-Xmx512m" in cleaned and "-XX:+UseG1GC" in cleaned
    assert "-XX:+UseConcMarkSweepGC" not in cleaned
    assert "-XX:MaxPermSize=256m" not in cleaned
    assert len(warns) == 2


def test_jvm_opts_reject_injection():
    cleaned, warns = jvm_opts.sanitize(["-Xmx1g; rm -rf /"], 17)
    assert cleaned == []
    assert warns and "unsafe" in warns[0]


# ---- registry ----
def test_registry_lines():
    assert registry.get_line("10").min_java == 11
    assert registry.get_line("11").min_java == 17
    assert registry.get_line("11").namespace == "jakarta"
    assert registry.get_line("9").legacy is True
    with pytest.raises(ValueError):
        registry.get_line("8")


def test_artifact_urls_use_fallback(monkeypatch):
    monkeypatch.setattr(registry, "resolve_latest_patch", lambda m, **k: "11.0.22")
    art = registry.artifact("11")
    assert art.tgz_url.endswith("apache-tomcat-11.0.22.tar.gz")
    assert art.sha512_url.endswith(".sha512")
    assert art.sig_url.endswith(".asc")
    assert art.min_java == 17


# ---- templating ----
def test_templating_render_and_missing():
    assert templating.render("port=@@p@@", {"p": "8080"}) == "port=8080"
    with pytest.raises(KeyError):
        templating.render("@@missing@@", {})


def test_server_xml_template_hardened():
    xml = templating.render_file("server.xml.tmpl", {"http_port": "8080"})
    assert 'address="127.0.0.1"' in xml
    assert 'shutdown="DISABLED"' in xml
    assert "AJP/1.3" not in xml


# ---- zip-slip safety ----
def _make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    buf.seek(0)
    return buf


def test_war_safe_extract_blocks_traversal(tmp_path):
    z = _make_zip([("../evil.txt", "x"), ("ok.txt", "y")])
    war_path = tmp_path / "a.war"
    war_path.write_bytes(z.read())
    with pytest.raises(war.UnsafeArchive):
        war.safe_extract(str(war_path), str(tmp_path / "out"))


def test_war_safe_extract_ok(tmp_path):
    z = _make_zip([("WEB-INF/web.xml", "<web/>"), ("index.jsp", "hi")])
    war_path = tmp_path / "b.war"
    war_path.write_bytes(z.read())
    out = tmp_path / "out"
    war.safe_extract(str(war_path), str(out))
    assert (out / "WEB-INF" / "web.xml").exists()


def test_namespace_warning(tmp_path):
    z = _make_zip([("WEB-INF/classes/javax/servlet/http/HttpServlet.class", "x")])
    war_path = tmp_path / "c.war"
    war_path.write_bytes(z.read())
    warn = war.namespace_warning(str(war_path), "jakarta")
    assert warn and "javax" in warn
