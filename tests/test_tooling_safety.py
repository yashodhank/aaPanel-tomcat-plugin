import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_requires_explicit_host_and_has_no_public_ip_default():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "VPS_HOST    ?=" not in makefile
    assert "217.217.248.180" not in makefile
    assert 'test -n "$(VPS_HOST)"' in makefile


def _fake_remote_tools(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "remote-command-ran"
    for command in ("rsync", "ssh"):
        tool = fake_bin / command
        tool.write_text(
            "#!/bin/sh\n"
            "touch \"%s\"\n"
            "exit 99\n" % marker,
            encoding="utf-8",
        )
        tool.chmod(0o755)
    env = os.environ.copy()
    env.pop("VPS_HOST", None)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    return env, marker


def test_deploy_without_host_fails_before_remote_commands(tmp_path):
    make = shutil.which("make")
    assert make, "make is required for the tooling contract test"
    env, marker = _fake_remote_tools(tmp_path)

    result = subprocess.run(
        [make, "deploy"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "VPS_HOST is required" in result.stdout + result.stderr
    assert not marker.exists()


def test_restart_without_host_fails_before_remote_commands(tmp_path):
    make = shutil.which("make")
    assert make, "make is required for the tooling contract test"
    env, marker = _fake_remote_tools(tmp_path)

    result = subprocess.run(
        [make, "restart"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "VPS_HOST is required" in result.stdout + result.stderr
    assert not marker.exists()


def test_deploy_dry_run_propagates_host_to_recursive_restart():
    make = shutil.which("make")
    assert make, "make is required for the tooling contract test"

    result = subprocess.run(
        [make, "-n", "deploy", "VPS_HOST=root@test-host"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "rsync " in output
    assert "root@test-host:/www/server/panel/plugin/javahost/" in output
    assert "ssh root@test-host " in output
