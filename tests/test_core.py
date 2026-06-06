# coding: utf-8
"""Offline unit tests for JavaHost pure-logic modules. No network, no panel."""
import io
import os
import zipfile

import pytest

from core.util import validate, immutable
from core import config
from core.runtime import java, jvm_opts
from core.tomcat import registry, templating, hardening, instance
from core.deploy import war, jar
from core.db import pg, mysql, mongo, engines as dbengines


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


def test_assert_no_ajp_ignores_commented_block(tmp_path):
    # Stock-style: AJP connector inside a multi-line comment -> must NOT raise.
    stock = tmp_path / "server.xml"
    stock.write_text(
        '<Server>\n'
        '  <!-- Define an AJP 1.3 Connector on port 8009\n'
        '  <Connector protocol="AJP/1.3" port="8009" redirectPort="8443" />\n'
        '  -->\n'
        '  <Connector port="8080" protocol="HTTP/1.1"/>\n'
        '</Server>\n'
    )
    hardening.assert_no_ajp(str(stock))  # no exception

    active = tmp_path / "active.xml"
    active.write_text('<Server><Connector protocol="AJP/1.3" port="8009"/></Server>')
    with pytest.raises(RuntimeError):
        hardening.assert_no_ajp(str(active))


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


# ---- PostgreSQL (all versions) ----
@pytest.mark.parametrize("raw,canon", [
    ("17", "17"), ("17.2", "17"), ("pg16", "16"), ("postgresql-15", "15"),
    ("9.6", "9.6"), ("PG 18", "18"), ("10", "10"),
])
def test_pg_normalize(raw, canon):
    assert pg.normalize_version(raw) == canon


def test_pg_normalize_rejects_unsupported():
    for bad in ("8.1", "7", "99", "", None, "rm"):
        with pytest.raises(ValueError):
            pg.normalize_version(bad)


def test_pg_supported_range():
    s = pg.supported()
    assert "9.4" in s and "17" in s and "18" in s
    assert "12" in s and "13" in s and "16" in s


def test_pg_driver_matrix():
    assert "42.7" in pg.recommend_driver(17)
    assert "42.7" in pg.recommend_driver(21)
    assert "42.2" in pg.recommend_driver(7)  # legacy JVM


def test_pg_jdbc_url():
    u = pg.jdbc_url("db.example.com", 5432, "appdb", version="16")
    assert u == "jdbc:postgresql://db.example.com:5432/appdb?sslmode=require"
    u2 = pg.jdbc_url("127.0.0.1", 5433, "x", ssl=False, params={"currentSchema": "app"})
    assert u2 == "jdbc:postgresql://127.0.0.1:5433/x?currentSchema=app"


def test_pg_jdbc_url_rejects_injection():
    with pytest.raises(ValueError):
        pg.jdbc_url("h", 5432, "db", params={"x": "a;DROP"})
    with pytest.raises(ValueError):
        pg.jdbc_url("h", 5432, "../etc")


def test_pg_env_no_secret_in_keys_and_driver_present():
    m = pg.render_env("app", host="h", port=5432, db="d", user="u",
                      password="s3cr3t", version="17", java_major=21)
    assert m["DB_URL"].startswith("jdbc:postgresql://")
    assert "42.7" in m["DB_DRIVER_MAVEN"]
    assert m["DB_PASSWORD"] == "s3cr3t"  # value carried, but written 0640 by write_env


# ---- MySQL / MariaDB ----
def test_mysql_url_and_driver():
    u = mysql.MYSQL.build_url("db", 3306, "appdb")
    assert u == "jdbc:mysql://db:3306/appdb?sslMode=REQUIRED"
    assert "mysql-connector-j:9" in mysql.MYSQL.recommend_driver(17)
    assert mysql.MYSQL.normalize("8.0.39") == "8.0"
    assert mysql.MYSQL.normalize("mysql-8.4") == "8.4"
    assert "5.7" in mysql.MYSQL.supported() and "9.1" in mysql.MYSQL.supported()


def test_mariadb_url_and_driver():
    u = mariadb_url = mysql.MARIADB.build_url("h", 3306, "d", ssl=False)
    assert u == "jdbc:mariadb://h:3306/d"
    assert "mariadb-java-client:3" in mysql.MARIADB.recommend_driver(21)
    assert mysql.MARIADB.normalize("11.4") == "11.4"
    assert mysql.MARIADB.normalize("10.11.8") == "10.11"
    with pytest.raises(ValueError):
        mysql.MARIADB.normalize("7.0")


# ---- MongoDB ----
def test_mongo_uri_and_env():
    u = mongo.ENGINE.build_url("h", 27017, "appdb")
    assert u == "mongodb://h:27017/appdb?tls=true"
    m = mongo.ENGINE.render_env(host="h", port=None, db="appdb", user="u",
                                password="s3cretZZ", version="7.0", java_major=21)
    assert m["DB_URL"].startswith("mongodb://h:27017/")
    assert "mongodb-driver-sync:5" in m["DB_DRIVER_MAVEN"]
    assert m["DB_PASSWORD"] == "s3cretZZ"  # carried separately
    assert "s3cretZZ" not in m["DB_URL"]   # never embedded in the URI
    assert mongo.ENGINE.normalize("6.0.13") == "6.0"


# ---- instance lifecycle helpers ----
def test_instance_base_path_validates(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    assert instance.base_path("my-app").endswith("/my-app")
    for bad in ("../etc", "a;b", ""):
        with pytest.raises(ValueError):
            instance.base_path(bad)


def test_instance_tail_and_readers(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    base = tmp_path / "app1"
    (base / "logs").mkdir(parents=True)
    (base / "conf").mkdir()
    (base / "bin").mkdir()
    (base / "logs" / "catalina.out").write_text("\n".join("line%d" % i for i in range(1, 51)) + "\n")
    (base / "conf" / "server.xml").write_text('<Connector port="8085" protocol="HTTP/1.1"/>')
    (base / "bin" / "setenv.sh").write_text('export JAVA_HOME="/x/jdk17"\nexport CATALINA_HOME="/y/tc11"\n')
    tail = instance.tail_log("app1", 5)
    assert tail.splitlines() == ["line46", "line47", "line48", "line49", "line50"]
    assert instance._read_port(str(base)) == 8085
    env = instance._read_setenv(str(base))
    assert env["JAVA_HOME"] == "/x/jdk17" and env["CATALINA_HOME"] == "/y/tc11"


def test_instance_list_apps(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    names = [a["app"] for a in instance.list_apps()]
    assert names == ["alpha", "beta"]


# ---- port allocation / conflict (B5) ----
def test_allocate_port(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(instance, "port_in_use", lambda p, host="127.0.0.1": False)
    base = tmp_path / "a" / "conf"
    base.mkdir(parents=True)
    (base / "server.xml").write_text('<Connector port="8080" protocol="HTTP/1.1"/>')
    assert instance.used_ports() == {8080: "a"}
    assert instance.allocate_port() == 8081           # 8080 already claimed
    assert instance.allocate_port(preferred=9000) == 9000
    with pytest.raises(RuntimeError):
        instance.allocate_port(preferred=8080)        # claimed -> conflict


def test_port_in_use_detects_bound():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); s.listen()
    port = s.getsockname()[1]
    try:
        assert instance.port_in_use(port) is True
    finally:
        s.close()


# ---- Jakarta migration tool ----
def test_migration_jar_name():
    assert war._MIGRATION_JAR % war.MIGRATION_VER == "jakartaee-migration-1.0.8-shaded.jar"


def test_migrate_missing_war(tmp_path):
    with pytest.raises(FileNotFoundError):
        war.migrate(str(tmp_path / "nope.war"), str(tmp_path / "out.war"), "/x")


# ---- executable / Spring Boot JAR detection ----
def _make_jar(tmp_path, name, main_class=None, extra=None):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        mf = "Manifest-Version: 1.0\n"
        if main_class:
            mf += "Main-Class: %s\n" % main_class
        z.writestr("META-INF/MANIFEST.MF", mf)
        for e in (extra or []):
            z.writestr(e, "x")
    return str(p)


def test_jar_executable_and_springboot(tmp_path):
    boot = _make_jar(tmp_path, "boot.jar",
                     main_class="org.springframework.boot.loader.JarLauncher",
                     extra=["BOOT-INF/classes/App.class"])
    plain = _make_jar(tmp_path, "plain.jar", main_class="com.example.Main")
    lib = _make_jar(tmp_path, "lib.jar")  # no Main-Class
    assert jar.is_executable_jar(boot) and jar.detect_springboot(boot)
    assert jar.is_executable_jar(plain) and not jar.detect_springboot(plain)
    assert not jar.is_executable_jar(lib)
    assert jar.manifest_main_class(plain) == "com.example.Main"


def test_read_port_jar_app(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    base = tmp_path / "japp" / "bin"
    base.mkdir(parents=True)
    (base / "app.env").write_text("SERVER_PORT=8090\nFOO=bar\n")
    assert instance._read_port(str(tmp_path / "japp")) == 8090


def test_health_no_port(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "dead").mkdir()
    h = instance.health("dead")
    assert h["up"] is False and h["port"] is None


# ---- system-hardening safe handling ----
def test_config_default_manage_hardening():
    assert config.get("manage_hardening") is True


def test_immutable_parse(monkeypatch):
    from core.util import shell as sh
    monkeypatch.setattr(immutable, "chattr_available", lambda: True)
    monkeypatch.setattr(sh, "run", lambda argv, **k: (0, "----i---------e------- /d\n", ""))
    assert immutable.is_immutable("/d") is True
    monkeypatch.setattr(sh, "run", lambda argv, **k: (0, "-------------e------- /d\n", ""))
    assert immutable.is_immutable("/d") is False


def test_immutable_writable_noop_when_not_immutable(monkeypatch):
    calls = []
    monkeypatch.setattr(immutable, "is_immutable", lambda p: False)
    monkeypatch.setattr(immutable, "_set", lambda f, p: calls.append((f, p)))
    with immutable.writable("/x") as lifted:
        assert lifted is False
    assert calls == []  # chattr never invoked


def test_immutable_writable_lifts_and_relocks(monkeypatch):
    calls = []
    monkeypatch.setattr(immutable, "is_immutable", lambda p: True)
    monkeypatch.setattr(immutable, "chattr_available", lambda: True)
    monkeypatch.setattr(immutable, "_set", lambda f, p: calls.append((f, p)))
    with immutable.writable("/x") as lifted:
        assert lifted is True
        assert calls == [("-i", "/x")]      # lifted inside the block
    assert calls == [("-i", "/x"), ("+i", "/x")]  # re-locked on exit


def test_immutable_writable_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(immutable, "is_immutable", lambda p: True)
    monkeypatch.setattr(immutable, "_set", lambda f, p: calls.append((f, p)))
    with immutable.writable("/x", enabled=False) as lifted:
        assert lifted is False
    assert calls == []  # respects manage_hardening=false


# ---- aaPanel daemon/exec protection (layer 2) detection ----
def test_verify_detects_bt_daemon_block(monkeypatch):
    from core.tomcat import service as svc
    monkeypatch.setattr(svc.time, "sleep", lambda s: None)

    def fake_run(argv, **k):
        if "is-active" in argv:
            return (3, "activating\n", "")
        if argv and argv[0] == "journalctl":
            return (0, "javahost-x.service: ... status=203/EXEC\nTips from BT security !!!\n", "")
        return (0, "", "")
    monkeypatch.setattr(svc.shell, "run", fake_run)
    with pytest.raises(RuntimeError) as e:
        svc._verify_systemd_started("x")
    assert "203/EXEC" in str(e.value) or "process/daemon protection" in str(e.value)


def test_verify_ok_when_active(monkeypatch):
    from core.tomcat import service as svc
    monkeypatch.setattr(svc.time, "sleep", lambda s: None)
    monkeypatch.setattr(svc.shell, "run",
                        lambda argv, **k: (0, "active\n", "") if "is-active" in argv else (0, "", ""))
    svc._verify_systemd_started("x")  # must not raise


# ---- syssafe allowlist merge (auto-whitelist) ----
def test_syssafe_merge_appends_missing():
    from core.compat import syssafe
    cfg = {"process": {"process_white": ["java"], "process_white_rule": ["/www/server/"]}}
    out, added = syssafe.merge_whitelist(cfg)
    assert "/www/server/javahost" in out["process"]["process_white_rule"]
    assert "catalina.sh" in out["process"]["process_white"]
    assert "/www/server/" in out["process"]["process_white_rule"]  # existing kept
    assert added  # reported what it added


def test_syssafe_exec_filter_detection(monkeypatch, tmp_path):
    from core.compat import syssafe
    f = tmp_path / "ld.so.preload"
    f.write_text("/usr/local/usranalyse/lib/libusranalyse.so\n")
    monkeypatch.setattr(syssafe, "LD_PRELOAD", str(f))
    r = syssafe.exec_filter()
    assert r["active"] is True and "usranalyse" in r["library"] and r["guidance"]
    f.write_text("")
    assert syssafe.exec_filter()["active"] is False


def test_syssafe_merge_idempotent():
    from core.compat import syssafe
    cfg = {"process": {}}
    cfg, _ = syssafe.merge_whitelist(cfg)
    cfg, added2 = syssafe.merge_whitelist(cfg)  # second pass
    assert added2 == []  # nothing new the second time


# ---- engine registry ----
def test_engine_registry():
    assert dbengines.get("postgres").name == "postgresql"
    assert dbengines.get("MariaDB").name == "mariadb"
    assert dbengines.get("mongo").name == "mongodb"
    with pytest.raises(ValueError):
        dbengines.get("oracle")
    names = {e["engine"] for e in dbengines.support_matrix()}
    assert names == {"postgresql", "mysql", "mariadb", "mongodb"}


def test_engine_default_port_used():
    # port omitted -> engine default
    m = mysql.MYSQL.render_env(host="h", port=None, db="d", user="u", password="x")
    assert ":3306/" in m["DB_URL"]


def test_namespace_warning(tmp_path):
    z = _make_zip([("WEB-INF/classes/javax/servlet/http/HttpServlet.class", "x")])
    war_path = tmp_path / "c.war"
    war_path.write_bytes(z.read())
    warn = war.namespace_warning(str(war_path), "jakarta")
    assert warn and "javax" in warn
