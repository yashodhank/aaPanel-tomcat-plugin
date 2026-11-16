# coding: utf-8
"""App creation, Tomcat install and job bookkeeping under concurrency and
failure. Service, JDK and network boundaries are stubbed; the filesystem and
the locks are real."""
import json
import os
import threading
import time
import zipfile

import pytest

from core import jobs
from core.runtime import java
from core.tomcat import installer, instance
from core.util import fs


@pytest.fixture
def host(tmp_path, monkeypatch):
    iroot = tmp_path / "instances"
    iroot.mkdir()
    monkeypatch.setattr(instance, "INSTANCE_ROOT", str(iroot))
    monkeypatch.setattr(fs, "MANAGED_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(installer, "is_installed", lambda major: "11.0.22")
    monkeypatch.setattr(installer, "home_path", lambda major: str(tmp_path / "tomcat" / str(major)))
    monkeypatch.setattr(installer, "ensure_java", lambda major, prefer=None: "/opt/jdk-17")
    monkeypatch.setattr(java, "probe", lambda home: 17)
    monkeypatch.setattr(instance, "port_in_use", lambda port: False)
    monkeypatch.setattr(instance.shell, "run", lambda *a, **k: (0, "", ""))
    calls = {"removed": [], "started": []}
    monkeypatch.setattr(instance.service, "write_setenv", lambda *a, **k: "/setenv")
    monkeypatch.setattr(instance.service, "install_unit", lambda *a, **k: "/unit")
    monkeypatch.setattr(instance.service, "install_jar_unit", lambda *a, **k: "/unit")
    monkeypatch.setattr(instance.service, "enable_start", lambda app: calls["started"].append(app))
    monkeypatch.setattr(instance.service, "remove_unit", lambda app: calls["removed"].append(app))
    monkeypatch.setattr(instance.service, "status", lambda app: "active")
    return iroot, calls


def _slow_allocate(monkeypatch):
    """Widen the window between choosing a port and claiming it."""
    real = instance.allocate_port

    def slow(*a, **k):
        port = real(*a, **k)
        time.sleep(0.05)
        return port
    monkeypatch.setattr(instance, "allocate_port", slow)


def _run_together(fns):
    errors, results = [], []

    def run(fn):
        try:
            results.append(fn())
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=run, args=(fn,)) for fn in fns]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results, errors


def test_concurrent_creates_never_share_a_port(host, monkeypatch):
    _slow_allocate(monkeypatch)
    results, errors = _run_together(
        [lambda n=n: instance.create("app%d" % n, "11", None, 256) for n in range(6)])
    assert errors == []
    ports = [r["port"] for r in results]
    assert len(set(ports)) == 6, ports


def test_concurrent_creates_of_one_name_yield_one_app(host, monkeypatch):
    _slow_allocate(monkeypatch)
    results, errors = _run_together([lambda: instance.create("same", "11", None, 256)] * 4)
    assert len(results) == 1 and len(errors) == 3
    assert all("already exists" in str(e) for e in errors)
    assert instance.exists("same")          # the winner was not cleaned up by a loser


@pytest.mark.parametrize("failing", ["install_unit", "enable_start"])
def test_failed_create_frees_the_name_and_the_port(host, monkeypatch, failing):
    iroot, calls = host

    def boom(*a, **k):
        raise RuntimeError("%s failed: /etc/systemd/system is immutable" % failing)
    monkeypatch.setattr(instance.service, failing, boom)
    with pytest.raises(RuntimeError, match="immutable"):
        instance.create("shop", "11", 8090, 256)
    assert not (iroot / "shop").exists() and calls["removed"] == ["shop"]
    assert 8090 not in instance.used_ports()

    monkeypatch.setattr(instance.service, failing, lambda *a, **k: None)
    assert instance.create("shop", "11", 8090, 256)["port"] == 8090      # retry just works


def test_create_refusing_an_existing_app_does_not_touch_it(host):
    iroot, calls = host
    instance.create("shop", "11", 8090, 256)
    marker = iroot / "shop" / "webapps" / "keep.txt"
    marker.write_text("live")
    with pytest.raises(RuntimeError, match="already exists"):
        instance.create("shop", "11", 8091, 256)
    assert marker.read_text() == "live" and calls["removed"] == []


# ---- JAR apps ---------------------------------------------------------------- #
@pytest.fixture
def jar(tmp_path):
    path = tmp_path / "app.jar"
    with zipfile.ZipFile(str(path), "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nMain-Class: demo.Main\n")
    return str(path)


def test_jar_app_gets_the_java_version_it_asked_for(host, jar, monkeypatch):
    installed = []
    monkeypatch.setattr(java, "detect", lambda: {21: "/opt/jdk-21"})
    monkeypatch.setattr(java, "install_temurin", lambda major: installed.append(major) or "/opt/jdk-%d" % major)
    res = instance.create_jar("legacy", jar, 8)
    assert installed == [8]                  # not silently run on the 21 that happens to be there
    env = open(os.path.join(instance.base_path("legacy"), "bin", "app.env")).read()
    assert "JAVA_HOME=/opt/jdk-8" in env and res["java"] == 8


def test_jar_app_uses_an_installed_exact_match(host, jar, monkeypatch):
    monkeypatch.setattr(java, "detect", lambda: {8: "/opt/jdk-8", 21: "/opt/jdk-21"})
    monkeypatch.setattr(java, "install_temurin", lambda major: pytest.fail("must not install"))
    instance.create_jar("legacy", jar, 8)
    assert "JAVA_HOME=/opt/jdk-8" in open(
        os.path.join(instance.base_path("legacy"), "bin", "app.env")).read()


def test_failed_jar_create_frees_the_name_and_the_port(host, jar, monkeypatch):
    iroot, calls = host
    monkeypatch.setattr(java, "detect", lambda: {17: "/opt/jdk-17"})

    def boom(*a, **k):
        raise RuntimeError("unit write refused")
    monkeypatch.setattr(instance.service, "install_jar_unit", boom)
    with pytest.raises(RuntimeError, match="refused"):
        instance.create_jar("svc", jar, 17, port=8095)
    assert not (iroot / "svc").exists() and 8095 not in instance.used_ports()


def test_concurrent_jar_creates_never_share_a_port(host, jar, monkeypatch):
    monkeypatch.setattr(java, "detect", lambda: {17: "/opt/jdk-17"})
    _slow_allocate(monkeypatch)
    results, errors = _run_together(
        [lambda n=n: instance.create_jar("svc%d" % n, jar, 17) for n in range(5)])
    assert errors == [] and len({r["port"] for r in results}) == 5


# ---- Tomcat install ---------------------------------------------------------- #
def test_one_tomcat_major_cannot_be_installed_twice_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "HOME_ROOT", str(tmp_path / "tomcat"))
    ran = []
    monkeypatch.setattr(installer, "_install_locked", lambda major, **k: ran.append(major) or {"major": major})
    with installer._major_lock("11"):
        with pytest.raises(RuntimeError, match="Tomcat 11 operation already in progress"):
            installer.install("11")
        with pytest.raises(RuntimeError, match="already in progress"):
            installer.uninstall("11")
        assert installer.install("10") == {"major": "10"}       # another major is independent
    assert installer.install("11") == {"major": "11"}           # released afterwards
    assert ran == ["10", "11"]


# ---- jobs across a reboot ---------------------------------------------------- #
@pytest.fixture
def jobroot(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", str(tmp_path / "jobs"))
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(jobs, "PROC_ROOT", str(proc))
    boot = 1_800_000_000.0
    (proc / "stat").write_text("cpu  1 2 3\nbtime %d\nprocesses 5\n" % boot)

    def add(job_id, state, started, pid=4242):
        jdir = os.path.join(jobs.JOBS_ROOT, job_id)
        jobs._write_meta(jdir, {"id": job_id, "kind": "install-tomcat", "target": "11",
                                "state": state, "started": started, "ended": None,
                                "message": "", "pid": pid})
        return jdir
    return add, boot, proc


def test_a_job_from_before_the_reboot_is_no_longer_running(jobroot):
    add, boot, _ = jobroot
    jdir = add("install-tomcat-old", "running", boot - 3600)
    listed = {j["id"]: j for j in jobs.list_jobs()}
    job = listed["install-tomcat-old"]
    assert job["state"] == "failed" and "server restarted" in job["message"]
    assert json.load(open(os.path.join(jdir, "meta.json")))["state"] == "failed"   # persisted
    with pytest.raises(ValueError, match="not running"):
        jobs.cancel("install-tomcat-old")
    assert jobs.clear() == 1                                    # and it can be cleared


def test_jobs_from_this_boot_are_left_alone(jobroot):
    add, boot, _ = jobroot
    add("after-boot", "running", boot + 30)
    add("clock-slack", "running", boot - 30)        # within the margin for a stepped clock
    add("done-before", "done", boot - 3600)
    states = {j["id"]: (j["state"], j["message"]) for j in jobs.list_jobs()}
    assert states == {"after-boot": ("running", ""), "clock-slack": ("running", ""),
                      "done-before": ("done", "")}


def test_unknown_boot_time_changes_nothing(jobroot):
    add, boot, proc = jobroot
    add("old", "running", boot - 3600)
    os.unlink(str(proc / "stat"))
    assert jobs.list_jobs()[0]["state"] == "running"
