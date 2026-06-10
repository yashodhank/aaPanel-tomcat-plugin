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
from core.deploy import war, jar, proxy, ssl, sitestatus
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


def _stub_list_apps_env(monkeypatch, tmp_path):
    """Isolate list_apps() from the host: no real systemctl / proc probes."""
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(instance.service, "status", lambda a: "inactive")
    monkeypatch.setattr(instance, "_instance_backend", lambda a: "systemd")
    monkeypatch.setattr(instance, "_is_enabled", lambda a, b, wants_cache=None: True)
    # list_apps now batches status via service.status_all() — stub it (and the
    # per-app fallback) so no real systemctl runs on the host.
    monkeypatch.setattr(instance.service, "status_all", lambda names: {})
    # list_apps no longer parses /proc for uptime (it stays None in the list);
    # stub metrics anyway so any stray call can't touch the host.
    monkeypatch.setattr(instance, "metrics", lambda a: {"uptime_s": 42})


def _mk_tomcat_instance(tmp_path, name, port, deploy=None):
    base = tmp_path / name
    (base / "conf").mkdir(parents=True)
    (base / "bin").mkdir()
    (base / "webapps").mkdir()
    (base / "conf" / "server.xml").write_text(
        '<Connector port="%d" protocol="HTTP/1.1"/>' % port)
    (base / "bin" / "setenv.sh").write_text(
        'export JAVA_HOME="/www/server/javahost/runtimes/jdk-17"\n'
        'export CATALINA_HOME="/www/server/javahost/tomcat/11"\n')
    if deploy:
        (base / "webapps" / deploy).mkdir()
    return base


def test_list_apps_tomcat_instance(monkeypatch, tmp_path):
    _stub_list_apps_env(monkeypatch, tmp_path)
    _mk_tomcat_instance(tmp_path, "site", 8085, deploy="ROOT")
    app = instance.list_apps()[0]
    assert app["app"] == "site"
    assert app["type"] == "war"            # has a deployed webapp
    assert app["port"] == 8085
    assert app["tomcat"] == 11
    assert app["java"] == 17
    assert app["runtime"] == "Tomcat 11"
    assert app["context"] == "/ROOT"
    assert app["backend"] == "systemd"
    assert app["enabled"] is True
    assert app["status"] == "inactive"


def test_list_apps_empty_tomcat_is_type_tomcat(monkeypatch, tmp_path):
    _stub_list_apps_env(monkeypatch, tmp_path)
    _mk_tomcat_instance(tmp_path, "blank", 8086)   # no webapp deployed
    app = instance.list_apps()[0]
    assert app["type"] == "tomcat"
    assert app["context"] is None
    assert app["port"] == 8086
    assert app["runtime"] == "Tomcat 11"


def test_list_apps_jar_instance(monkeypatch, tmp_path):
    _stub_list_apps_env(monkeypatch, tmp_path)
    base = tmp_path / "boot"
    (base / "bin").mkdir(parents=True)
    (base / "app.jar").write_text("PK\x03\x04")     # marker file is enough
    (base / "bin" / "app.env").write_text("SERVER_PORT=8090\n")
    (base / "bin" / "setenv.sh").write_text(
        'export JAVA_HOME="/www/server/javahost/runtimes/jdk-21"\n')
    app = instance.list_apps()[0]
    assert app["type"] == "jar"
    assert app["port"] == 8090
    assert app["java"] == 21
    assert app["runtime"] == "Java 21"
    assert app["tomcat"] is None
    assert app["context"] is None


def test_list_apps_malformed_dir_still_listed(monkeypatch, tmp_path):
    _stub_list_apps_env(monkeypatch, tmp_path)
    (tmp_path / "broken").mkdir()                   # empty: no conf, bin, jar
    apps = instance.list_apps()
    assert len(apps) == 1
    app = apps[0]
    assert app["app"] == "broken"
    assert app["status"] == "inactive"             # status always present
    assert app["type"] == "tomcat"                 # no jar, empty webapps
    assert app["port"] is None
    assert app["tomcat"] is None and app["java"] is None
    assert app["runtime"] is None


def test_list_apps_one_bad_app_does_not_break_list(monkeypatch, tmp_path):
    _stub_list_apps_env(monkeypatch, tmp_path)

    def boom(name):
        if name == "bad":
            raise RuntimeError("kaboom")
        return "active"

    monkeypatch.setattr(instance.service, "status", boom)
    (tmp_path / "bad").mkdir()
    _mk_tomcat_instance(tmp_path, "good", 8087, deploy="ROOT")
    apps = {a["app"]: a for a in instance.list_apps()}
    assert set(apps) == {"bad", "good"}
    assert apps["bad"]["status"] == "unknown"      # swallowed, still listed
    assert apps["good"]["status"] == "active"


def test_list_apps_full_key_set(monkeypatch, tmp_path):
    _stub_list_apps_env(monkeypatch, tmp_path)
    _mk_tomcat_instance(tmp_path, "site", 8088, deploy="ROOT")
    base = tmp_path / "boot"
    (base / "bin").mkdir(parents=True)
    (base / "app.jar").write_text("x")
    (base / "bin" / "app.env").write_text("SERVER_PORT=8091\n")
    expected = {"app", "type", "status", "runtime", "tomcat", "java",
                "port", "context", "enabled", "backend", "uptime", "domain", "ssl",
                "runtime_ok"}
    for app in instance.list_apps():
        assert set(app) == expected


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


# ---- is-enabled: filesystem symlink check (no systemctl subprocess) ----
def test_is_enabled_systemd_true_when_wants_symlink(monkeypatch, tmp_path):
    """An enabled systemd unit has a symlink under multi-user.target.wants/.
    Detected by stat, never by spawning systemctl."""
    sysd = tmp_path / "systemd"
    wants = sysd / "multi-user.target.wants"
    wants.mkdir(parents=True)
    unit = sysd / "javahost-site.service"
    unit.write_text("[Unit]\n")
    (wants / "javahost-site.service").symlink_to(unit)
    # blow up if anything tries to spawn a subprocess
    monkeypatch.setattr(instance.shell, "run",
                        lambda *a, **k: pytest.fail("subprocess spawned"))
    monkeypatch.setattr(instance.service, "SYSTEMD_DIR", str(sysd))
    assert instance._is_enabled("site", "systemd") is True


def test_is_enabled_systemd_false_when_no_symlink(monkeypatch, tmp_path):
    sysd = tmp_path / "systemd"
    (sysd / "multi-user.target.wants").mkdir(parents=True)
    monkeypatch.setattr(instance.shell, "run",
                        lambda *a, **k: pytest.fail("subprocess spawned"))
    monkeypatch.setattr(instance.service, "SYSTEMD_DIR", str(sysd))
    assert instance._is_enabled("ghost", "systemd") is False


# ---- batched health (eliminates the per-app GetHealth N+1) ----
def test_health_all_shape_and_tolerates_bad_app(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "ok").mkdir()
    (tmp_path / "boom").mkdir()
    (tmp_path / "afile").write_text("not a dir")  # ignored: not an instance dir

    def fake_health(app, timeout=2.0):
        if app == "boom":
            raise RuntimeError("kaboom")
        return {"app": app, "up": True, "code": 200, "port": 8080}

    monkeypatch.setattr(instance, "health", fake_health)
    res = instance.health_all()
    assert set(res) == {"ok", "boom"}
    assert res["ok"] == {"up": True, "code": 200, "port": 8080}
    assert res["boom"] == {"up": False, "code": None, "port": None}
    for v in res.values():
        assert set(v) == {"up", "code", "port"}


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


# ---- app metrics / pid resolution ----
def test_resolve_pid_from_systemd(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(instance.shell, "run",
                        lambda argv, **k: (0, "4321\n", "") if "MainPID" in argv else (0, "", ""))
    assert instance._resolve_pid("app1") == 4321


def test_metrics_down_when_no_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(instance, "_resolve_pid", lambda a: None)
    m = instance.metrics("dead")
    assert m["up"] is False and m["pid"] is None


def test_metrics_reads_proc(monkeypatch, tmp_path):
    import os as _os
    if not _os.path.isdir("/proc"):
        pytest.skip("needs /proc (Linux)")
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(instance, "_resolve_pid", lambda a: _os.getpid())
    m = instance.metrics("self")
    assert m["up"] and m["rss_mb"] and m["threads"] >= 1


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


# ---- per-site SSL: vhost rendering, marker round-trip, enable orchestration ----
def test_write_vhost_ssl_renders_443_and_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path))
    path = proxy.write_vhost("site", "app.example.com", 8085, ssl=True)
    conf = open(path).read()
    # a 443 TLS server with the LE live cert
    assert "listen 443 ssl;" in conf
    assert "listen [::]:443 ssl;" in conf
    assert "ssl_certificate /etc/letsencrypt/live/app.example.com/fullchain.pem;" in conf
    assert "ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;" in conf
    # port-80 server redirects to https
    assert "return 301 https://$host$request_uri;" in conf
    # ACME challenge location is still present (renewal must keep working)
    assert "location ^~ /.well-known/acme-challenge/" in conf
    assert ("root %s;" % proxy.ACME_WEBROOT) in conf
    # https proxy forwards the right scheme + targets the backend
    assert "proxy_pass http://127.0.0.1:8085;" in conf
    assert "proxy_set_header X-Forwarded-Proto https;" in conf


def test_write_vhost_http_default_has_acme_no_443(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path))
    path = proxy.write_vhost("site", "app.example.com", 8085)  # old 3-arg caller, ssl defaults False
    conf = open(path).read()
    assert "listen 80;" in conf and "listen [::]:80;" in conf
    assert "listen 443 ssl;" not in conf
    assert "location ^~ /.well-known/acme-challenge/" in conf
    assert "proxy_pass http://127.0.0.1:8085;" in conf


def test_ssl_read_marker_roundtrip(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "site" / "bin").mkdir(parents=True)
    assert ssl.read_ssl("site") is False
    ssl._mark_ssl("site", True)
    assert ssl.read_ssl("site") is True
    ssl._mark_ssl("site", False)
    assert ssl.read_ssl("site") is False


def test_ssl_enable_falls_back_to_certbot(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "ACME_WEBROOT", str(tmp_path / "acme"))
    (tmp_path / "site" / "bin").mkdir(parents=True)

    # never touch nginx / the panel / certbot / /etc
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(ssl, "_install_renewal_hook", lambda: None)
    monkeypatch.setattr(ssl, "_aapanel_apply", lambda domain: False)   # native unavailable
    monkeypatch.setattr(ssl, "_certbot_issue",
                        lambda domain, email=None: (True, None))  # certbot succeeds
    monkeypatch.setattr(ssl, "_cert_exists", lambda domain: True)      # cert now present
    monkeypatch.setattr(ssl, "_cert_not_after", lambda domain: None)   # skip openssl

    res = ssl.enable("site", "app.example.com", 8085)
    assert res == {"ssl": True, "url": "https://app.example.com/", "via": "certbot"}
    assert ssl.read_ssl("site") is True
    # vhost was rewritten to the SSL variant
    conf = open(proxy.vhost_path("site")).read()
    assert "listen 443 ssl;" in conf


def test_ssl_enable_reports_failure_when_no_cert(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "ACME_WEBROOT", str(tmp_path / "acme"))
    (tmp_path / "site" / "bin").mkdir(parents=True)
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(ssl, "_aapanel_apply", lambda domain: False)
    monkeypatch.setattr(ssl, "_certbot_issue",
                        lambda domain, email=None: (False, "rate limited"))
    monkeypatch.setattr(ssl, "_cert_exists", lambda domain: False)

    res = ssl.enable("site", "app.example.com", 8085)
    assert res["ssl"] is False and "error" in res
    assert "rate limited" in res["error"]  # certbot stderr surfaced (M5)
    assert ssl.read_ssl("site") is False
    # HTTP vhost left in place (no 443 server)
    conf = open(proxy.vhost_path("site")).read()
    assert "listen 443 ssl;" not in conf


# ---- aaPanel add-site: false status must fall back to nginx (H1/H2) ----------
def test_set_site_errors_when_aapanel_api_fails(tmp_path, monkeypatch):
    """When all aaPanel API paths fail, set_site falls back to nginx vhost with warning."""
    vdir = str(tmp_path / "vhost")
    monkeypatch.setattr(proxy, "VHOST_DIR", vdir)
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": False, "path": "aapanel",
                                      "detail": "all paths failed",
                                      "tried": ["class-api", "legacy-panelsite"]})

    res = proxy.set_site("demo", "demo.example.com", 8080)
    assert res["via"] == "nginx-vhost"
    assert "warning" in res
    assert "does NOT appear in aaPanel" in res["warning"]
    assert os.path.isfile(os.path.join(vdir, "demo.conf"))


def test_set_site_aapanel_true_status_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: False)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "_store_domain", lambda app, dom: None)
    monkeypatch.setattr(proxy, "aapanel_add_site",
                        lambda d, p: {"ok": True, "path": "aapanel",
                                      "detail": "via panelSite.CreateProxy"})
    res = proxy.set_site("demo", "demo.example.com", 8081)
    assert res["via"] == "aapanel"
    assert "warning" not in res
    assert not os.path.isfile(os.path.join(str(tmp_path / "vhost"), "demo.conf"))


# ---- ssl.disable reverts to HTTP and clears the marker ----------------------
def test_ssl_disable_reverts_to_http(tmp_path, monkeypatch):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    (tmp_path / "site" / "bin").mkdir(parents=True)
    ssl._mark_ssl("site", True)
    assert ssl.read_ssl("site") is True
    res = ssl.disable("site", "app.example.com", 8085)
    assert res == {"ssl": False, "url": "http://app.example.com/"}
    assert ssl.read_ssl("site") is False
    conf = open(proxy.vhost_path("site")).read()
    assert "listen 443 ssl;" not in conf
    assert "proxy_pass http://127.0.0.1:8085;" in conf


# ---- ssl.enable native-success path stores cert not_after in the marker -----
def test_ssl_enable_marker_carries_not_after(tmp_path, monkeypatch):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    monkeypatch.setattr(proxy, "ACME_WEBROOT", str(tmp_path / "acme"))
    (tmp_path / "site" / "bin").mkdir(parents=True)
    monkeypatch.setattr(proxy, "ensure_include", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(ssl, "_install_renewal_hook", lambda: None)
    monkeypatch.setattr(ssl, "_aapanel_apply", lambda domain: True)   # native succeeds
    monkeypatch.setattr(ssl, "_cert_exists", lambda domain: True)
    monkeypatch.setattr(ssl, "_cert_not_after", lambda domain: "2026-09-01T00:00:00Z")

    res = ssl.enable("site", "app.example.com", 8085)
    assert res["ssl"] is True and res["via"] == "aapanel"
    assert res["not_after"] == "2026-09-01T00:00:00Z"
    marker = open(ssl._ssl_marker("site")).read()
    assert "2026-09-01T00:00:00Z" in marker
    assert ssl.read_ssl("site") is True


# ---- site_suffix de-hardcode -------------------------------------------------
def test_site_suffix_default_empty_and_default_domain_none(monkeypatch):
    # no config file -> empty suffix -> no synthesized domain
    monkeypatch.setattr(config, "site_suffix", lambda: "")
    assert proxy.default_domain("myapp") is None


def test_default_domain_uses_configured_suffix(monkeypatch):
    monkeypatch.setattr(config, "site_suffix", lambda: "example.com")
    assert proxy.default_domain("myapp") == "myapp.example.com"


def test_setsite_errors_without_domain_or_suffix(monkeypatch):
    import javahost_main
    monkeypatch.setattr(javahost_main.config, "site_suffix", lambda: "")
    monkeypatch.setattr(javahost_main.proxy, "default_domain", lambda app: None)

    class G(object):
        app = "demo"
        domain = None

    res = javahost_main.javahost_main().SetSite(G())
    assert res.get("status") is False
    assert "no domain" in (res.get("msg") or "")


# ---- sitestatus.probe shape (openssl + urllib monkeypatched) ----------------
def test_sitestatus_probe_full_shape(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "site" / "bin").mkdir(parents=True)

    monkeypatch.setattr(sitestatus.instance, "health",
                        lambda app, **k: {"app": app, "up": True, "code": 200, "port": 8085})
    monkeypatch.setattr(sitestatus.proxy, "read_domain", lambda app: "app.example.com")
    monkeypatch.setattr(sitestatus.ssl, "read_ssl", lambda app: True)
    # cert file present + openssl returns a parseable enddate
    monkeypatch.setattr(sitestatus.ssl, "_live_fullchain",
                        lambda d: str(tmp_path / "fullchain.pem"))
    (tmp_path / "fullchain.pem").write_text("x")
    monkeypatch.setattr(sitestatus.shell, "run",
                        lambda argv, **k: (0, "notAfter=Sep  1 00:00:00 2099 GMT\n", ""))
    monkeypatch.setattr(sitestatus, "_probe_http",
                        lambda d: {"code": 301, "redirects_to_https": True})
    monkeypatch.setattr(sitestatus, "_probe_https",
                        lambda d: {"reachable": True, "code": 200})

    res = sitestatus.probe("site", probe_site=True)
    assert set(res) == {"app", "health", "domain", "ssl_marker", "cert", "site"}
    assert res["domain"] == "app.example.com"
    assert res["ssl_marker"] is True
    assert res["cert"]["exists"] is True
    assert res["cert"]["not_after"].startswith("2099-09-01")
    assert res["cert"]["valid"] is True and res["cert"]["days_left"] > 0
    assert res["site"]["http"] == {"code": 301, "redirects_to_https": True}
    assert res["site"]["https"] == {"reachable": True, "code": 200}


def test_sitestatus_probe_no_domain(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "site" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sitestatus.instance, "health",
                        lambda app, **k: {"app": app, "up": False, "code": None, "port": None})
    monkeypatch.setattr(sitestatus.proxy, "read_domain", lambda app: None)
    monkeypatch.setattr(sitestatus.ssl, "read_ssl", lambda app: False)
    res = sitestatus.probe("site", probe_site=True)
    assert res["domain"] is None
    assert res["cert"] is None      # no domain -> no cert lookup
    assert res["site"] is None      # no domain -> nothing to reach


def test_sitestatus_probe_site_skipped_when_false(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    (tmp_path / "site" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sitestatus.instance, "health",
                        lambda app, **k: {"app": app, "up": True, "code": 200, "port": 8085})
    monkeypatch.setattr(sitestatus.proxy, "read_domain", lambda app: "app.example.com")
    monkeypatch.setattr(sitestatus.ssl, "read_ssl", lambda app: False)
    monkeypatch.setattr(sitestatus.ssl, "_live_fullchain", lambda d: str(tmp_path / "none.pem"))

    def _boom(d):
        raise AssertionError("network probe ran despite probe_site=False")

    monkeypatch.setattr(sitestatus, "_probe_http", _boom)
    monkeypatch.setattr(sitestatus, "_probe_https", _boom)
    res = sitestatus.probe("site", probe_site=False)
    assert res["site"] is None
    assert res["cert"] is None      # no cert file


# ---- StartAppAction returns a job_id (jobs.start monkeypatched) --------------
def test_start_app_action_returns_job_id(monkeypatch):
    import javahost_main
    captured = {}

    def fake_start(kind, target, argv):
        captured["kind"] = kind
        captured["target"] = target
        captured["argv"] = argv
        return "app-restart-20260606T000000Z-abc123"

    monkeypatch.setattr(javahost_main.jobs, "start", fake_start)

    class G(object):
        app = "demo"
        action = "restart"

    res = javahost_main.javahost_main().StartAppAction(G())
    assert res["status"] is True
    assert res["msg"]["job_id"] == "app-restart-20260606T000000Z-abc123"
    assert res["msg"]["app"] == "demo" and res["msg"]["action"] == "restart"
    assert captured["kind"] == "app-restart" and captured["target"] == "demo"


def test_start_app_action_rejects_bad_action(monkeypatch):
    import javahost_main
    monkeypatch.setattr(javahost_main.jobs, "start",
                        lambda *a, **k: pytest.fail("job started for invalid action"))

    class G(object):
        app = "demo"
        action = "obliterate"

    res = javahost_main.javahost_main().StartAppAction(G())
    assert res.get("status") is False
    assert "invalid action" in (res.get("msg") or "")


# ---- Java uninstall: panel-path refusal + dependents block + force ----------
def _mk_instance_with_java(tmp_path, name, java_home):
    base = tmp_path / name / "bin"
    base.mkdir(parents=True)
    (base / "setenv.sh").write_text('export JAVA_HOME="%s"\n' % java_home)
    return base


def test_java_uninstall_refuses_panel_path(monkeypatch):
    # major 17 resolves to the panel-owned /usr/local/btjdk -> refuse.
    monkeypatch.setattr(java, "detect", lambda: {17: "/usr/local/btjdk/jdk17"})
    with pytest.raises(RuntimeError) as e:
        java.uninstall(17)
    assert "panel-managed" in str(e.value)


def test_java_uninstall_blocks_on_dependents_then_force(monkeypatch, tmp_path):
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    # an app pins jdk-8 under the plugin runtimes
    _mk_instance_with_java(tmp_path, "legacyapp",
                           "/www/server/javahost/runtimes/jdk-8")
    monkeypatch.setattr(java, "detect",
                        lambda: {8: "/www/server/javahost/runtimes/jdk-8"})

    assert java.usage(8) == ["legacyapp"]

    # blocked (not silent) without force
    with pytest.raises(RuntimeError) as e:
        java.uninstall(8)
    assert "in use" in str(e.value) and "legacyapp" in str(e.value)

    # with force it proceeds; intercept the actual removal so nothing is deleted
    removed = {}
    monkeypatch.setattr(java.fs, "is_managed", lambda p: True)
    monkeypatch.setattr(java.fs, "safe_rmtree",
                        lambda p, **k: removed.update(path=p))
    monkeypatch.setattr(java.os.path, "isdir",
                        lambda p: p == "/www/server/javahost/runtimes/jdk-8")
    res = java.uninstall(8, force=True)
    assert res["removed"] is True and res["forced"] is True
    assert removed["path"] == "/www/server/javahost/runtimes/jdk-8"


def test_java_usage_endpoint(monkeypatch):
    import javahost_main
    monkeypatch.setattr(javahost_main.java, "usage", lambda m: ["a", "b"])

    class G(object):
        version = "17"

    res = javahost_main.javahost_main().GetJavaUsage(G())
    assert res["status"] is True
    assert res["msg"] == {"version": 17, "in_use_by": ["a", "b"]}


def test_uninstall_java_endpoint_blocks_with_in_use_list(monkeypatch):
    import javahost_main
    monkeypatch.setattr(javahost_main.java, "usage", lambda m: ["webapp"])
    monkeypatch.setattr(javahost_main.java, "uninstall",
                        lambda *a, **k: pytest.fail("uninstall ran despite dependents"))

    class G(object):
        version = "17"
        force = "0"

    res = javahost_main.javahost_main().UninstallJava(G())
    assert res["status"] is False
    assert res["msg"]["in_use_by"] == ["webapp"]


# ---- maintenance.wipe_preview / wipe ----------------------------------------
def test_wipe_preview_shape_lists_plugin_jdks_not_panel(monkeypatch, tmp_path):
    from core import maintenance
    from core.tomcat import instance as inst, installer as instr
    # apps
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path / "instances"))
    (tmp_path / "instances" / "app1").mkdir(parents=True)
    # plugin jdks: jdk-8, jdk-17 present; an unrelated dir must NOT be listed
    monkeypatch.setattr(maintenance.java, "JDK_ROOT", str(tmp_path / "runtimes"))
    for d in ("jdk-8", "jdk-17", "notajdk"):
        (tmp_path / "runtimes" / d).mkdir(parents=True)
    # tomcats: only major 11 installed
    monkeypatch.setattr(maintenance, "_list_installed_tomcats", lambda: ["11"])
    # sites under VHOST_DIR
    monkeypatch.setattr(maintenance.proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    (tmp_path / "vhost").mkdir()
    (tmp_path / "vhost" / "site.conf").write_text("server {}\n")
    monkeypatch.setattr(maintenance, "DATA_ROOT", str(tmp_path / "javahost"))

    res = maintenance.wipe_preview()
    assert set(res) == {"apps", "jdks", "tomcats", "sites",
                        "data_root", "confirm_required"}
    assert res["apps"]["items"] == ["app1"]
    # plugin jdks only — the panel /usr/local/btjdk is never scanned, and the
    # non-jdk dir is excluded
    assert res["jdks"]["items"] == ["jdk-17", "jdk-8"]
    assert "/usr/local/btjdk" not in str(res["jdks"])
    assert res["tomcats"]["items"] == ["11"]
    assert res["sites"]["items"] == ["site.conf"]
    assert res["data_root"]["path"].endswith("/javahost")


def test_wipe_noop_when_confirm_wrong(monkeypatch):
    from core import maintenance
    monkeypatch.setattr(maintenance, "_wipe_apps",
                        lambda: pytest.fail("wipe ran without confirm"))
    res = maintenance.wipe(["apps"], confirm="nope")
    assert res["performed"] is False
    assert "confirmation required" in res["reason"]


def test_wipe_sites_calls_remove_site(monkeypatch, tmp_path):
    from core import maintenance
    monkeypatch.setattr(maintenance.proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    (tmp_path / "vhost").mkdir()
    (tmp_path / "vhost" / "demo.conf").write_text("server {}\n")
    (tmp_path / "vhost" / "shop.conf").write_text("server {}\n")
    calls = []
    monkeypatch.setattr(maintenance.proxy, "remove_site",
                        lambda app: calls.append(app) or {"app": app, "removed": True})
    monkeypatch.setattr(maintenance, "_remove_include", lambda *a, **k: True)
    monkeypatch.setattr(maintenance.proxy, "reload_nginx", lambda *a, **k: True)

    res = maintenance.wipe(["sites"], confirm="WIPE")
    assert res["performed"] is True
    assert sorted(calls) == ["demo", "shop"]
    assert sorted(res["steps"]["sites"]["removed"]) == ["demo", "shop"]
    assert res["steps"]["sites"]["include_removed"] is True


def test_wipe_rejects_bad_scope():
    from core import maintenance
    with pytest.raises(ValueError):
        maintenance.wipe("apps,bogus", confirm="WIPE")


# ---- aaPanel HTTP API site registration -----------------------------------
def test_aapanel_add_site_http_api_succeeds(monkeypatch):
    """When class API and legacy fail, HTTP API succeeds."""
    monkeypatch.setattr(proxy, "_try_aapanel_class_api", lambda d, p: None)
    monkeypatch.setattr(proxy, "_try_legacy_panelSite_import", lambda d, p: None)
    monkeypatch.setattr(proxy, "_try_aapanel_http_api",
                        lambda d, p: {"ok": True, "path": "aapanel-http",
                                      "detail": "via HTTP AddSite"})
    monkeypatch.setattr(proxy.config, "aapanel_api_key", lambda: "fake-key")

    res = proxy.aapanel_add_site("test.example.com", 8080)
    assert res["ok"] is True
    assert res["path"] == "aapanel-http"


def test_aapanel_add_site_all_paths_fail(monkeypatch):
    """All 3 tiers fail — returns ok=False with tried paths."""
    monkeypatch.setattr(proxy, "_try_aapanel_class_api", lambda d, p: None)
    monkeypatch.setattr(proxy, "_try_legacy_panelSite_import", lambda d, p: None)
    monkeypatch.setattr(proxy, "_try_aapanel_http_api", lambda d, p: None)
    # Ensure api_sk is set so http-api path is attempted
    monkeypatch.setattr(proxy.config, "aapanel_api_key", lambda: "fake-key")

    res = proxy.aapanel_add_site("test.example.com", 8080)
    assert res["ok"] is False
    assert "none succeeded" in res["detail"]
    assert res["tried"] == ["class-api", "legacy-panelsite", "http-api"]


def test_aapanel_remove_site_http_succeeds(monkeypatch):
    """HTTP API removes the site."""
    monkeypatch.setattr(proxy, "_aapanel_http_remove_site", lambda d: True)
    assert proxy._aapanel_http_remove_site("test.example.com") is True


def test_aapanel_remove_site_falls_to_class_api(monkeypatch):
    """HTTP fails, class API succeeds."""
    import sys
    monkeypatch.setattr(proxy, "_aapanel_http_remove_site", lambda d: False)
    monkeypatch.setattr(sys, "path", sys.path + ["/fake/panel/class"])

    class _FakeSiteObj:
        def DeleteSite(self, g):
            return {"status": True, "msg": "ok"}

    class _FakeSiteMod:
        @staticmethod
        def site():
            return _FakeSiteObj()

    monkeypatch.setitem(sys.modules, "site", _FakeSiteMod())
    monkeypatch.setattr(proxy, "AAPANEL_PANEL_CLASS", "/fake/panel/class")

    removed = proxy.aapanel_remove_site("test.example.com")
    assert removed is True


# ---- remove_site aaPanel cleanup ---------------------------------------------
def test_remove_site_reports_aapanel_cleaned(monkeypatch, tmp_path):
    from core.tomcat import instance
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    marker_dir = str(tmp_path / "demo" / "bin")
    os.makedirs(marker_dir, exist_ok=True)
    proxy._store_domain("demo", "demo.example.com")

    vdir = str(tmp_path / "vhost")
    monkeypatch.setattr(proxy, "VHOST_DIR", vdir)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "aapanel_remove_site", lambda d: True)

    os.makedirs(vdir, exist_ok=True)
    open(os.path.join(vdir, "demo.conf"), "w").write("server {}\n")

    res = proxy.remove_site("demo")
    assert res["removed"] is True
    assert res["aapanel_cleaned"] is True
    assert proxy.read_domain("demo") is None


def test_remove_site_aapanel_not_found_is_ok(monkeypatch, tmp_path):
    from core.tomcat import instance
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(tmp_path))
    marker_dir = str(tmp_path / "demo" / "bin")
    os.makedirs(marker_dir, exist_ok=True)
    proxy._store_domain("demo", "demo.example.com")

    vdir = str(tmp_path / "vhost")
    monkeypatch.setattr(proxy, "VHOST_DIR", vdir)
    monkeypatch.setattr(proxy, "reload_nginx", lambda *a, **k: True)
    monkeypatch.setattr(proxy, "aapanel_remove_site", lambda d: False)

    os.makedirs(vdir, exist_ok=True)
    open(os.path.join(vdir, "demo.conf"), "w").write("server {}\n")

    res = proxy.remove_site("demo")
    assert res["removed"] is True
    assert res["aapanel_cleaned"] is False
    assert proxy.read_domain("demo") is None


# ---- DeleteApp calls remove_site ---------------------------------------------
def test_delete_app_removes_site(monkeypatch, tmp_path):
    import sys
    from core.tomcat import instance as inst, service
    from core.util import fs as util_fs
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path))
    app_dir = tmp_path / "myapp"
    app_dir.mkdir(parents=True)
    util_fs.mark_managed(str(app_dir))
    monkeypatch.setattr(service, "remove_unit", lambda app: None)
    # safe_rmtree rejects /tmp paths — replace with simple rmtree
    monkeypatch.setattr(util_fs, "safe_rmtree",
                        lambda path, **k: __import__("shutil").rmtree(path))

    calls = []
    class _FakeMod:
        @staticmethod
        def remove_site(app):
            calls.append(app)
            return {"app": app, "removed": True, "aapanel_cleaned": True}

    monkeypatch.setitem(sys.modules, "core.deploy.proxy", _FakeMod)
    # core.deploy package already has `proxy` as real module; replace it
    if "core.deploy" in sys.modules:
        monkeypatch.setattr(sys.modules["core.deploy"], "proxy", _FakeMod)

    res = inst.delete("myapp")
    assert res["removed"] is True
    assert calls == ["myapp"]
