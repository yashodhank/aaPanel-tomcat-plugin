# coding: utf-8
"""
Service management (closes B1/F4 — JAVA_HOME comes from env, never parsed from
a shebang). Prefers systemd; falls back to a lint-clean init.d script on hosts
without systemd. Both consume the same setenv.sh as the single source of truth.
"""
from __future__ import annotations

import os
from typing import Dict, List

from . import templating
from ..util import shell, fs

SYSTEMD_DIR = "/etc/systemd/system"
INITD_DIR = "/etc/init.d"


def have_systemd() -> bool:
    return bool(shell.which("systemctl")) and os.path.isdir("/run/systemd/system")


def _ctx(app: str, java_home: str, catalina_home: str, catalina_base: str,
         user: str = "www") -> Dict[str, str]:
    return {
        "app": app,
        "user": user,
        "group": user,
        "java_home": java_home,
        "catalina_home": catalina_home,
        "catalina_base": catalina_base,
    }


def install_unit(app: str, java_home: str, catalina_home: str, catalina_base: str,
                 user: str = "www") -> str:
    """Render and install the service unit. Returns the unit/script path."""
    ctx = _ctx(app, java_home, catalina_home, catalina_base, user)
    if have_systemd():
        unit_path = os.path.join(SYSTEMD_DIR, "javahost-%s.service" % app)
        fs.atomic_write(unit_path, templating.render_file("systemd.service.tmpl", ctx), mode=0o644)
        shell.run(["systemctl", "daemon-reload"])
        return unit_path
    script = os.path.join(INITD_DIR, "javahost-%s" % app)
    fs.atomic_write(script, templating.render_file("initd.sh.tmpl", ctx), mode=0o755)
    return script


def _systemd(*args: str) -> None:
    shell.run(["systemctl"] + list(args))


def enable_start(app: str) -> None:
    if have_systemd():
        _systemd("enable", "--now", "javahost-%s.service" % app)
    else:
        shell.run([os.path.join(INITD_DIR, "javahost-%s" % app), "start"])


def action(app: str, what: str) -> None:
    if what not in ("start", "stop", "restart"):
        raise ValueError("bad action: %r" % what)
    if have_systemd():
        _systemd(what, "javahost-%s.service" % app)
    else:
        shell.run([os.path.join(INITD_DIR, "javahost-%s" % app), what])


def status(app: str) -> str:
    if have_systemd():
        rc, out, _ = shell.run(
            ["systemctl", "is-active", "javahost-%s.service" % app], check=False)
        return out.strip() or "unknown"
    script = os.path.join(INITD_DIR, "javahost-%s" % app)
    if not os.path.exists(script):
        return "absent"
    rc, out, _ = shell.run([script, "status"], check=False)
    return "active" if rc == 0 else "inactive"


def remove_unit(app: str) -> None:
    if have_systemd():
        unit = "javahost-%s.service" % app
        shell.run(["systemctl", "disable", "--now", unit], check=False)
        path = os.path.join(SYSTEMD_DIR, unit)
        if os.path.exists(path):
            os.unlink(path)
        shell.run(["systemctl", "daemon-reload"], check=False)
    else:
        path = os.path.join(INITD_DIR, "javahost-%s" % app)
        if os.path.exists(path):
            shell.run([path, "stop"], check=False)
            os.unlink(path)


def write_setenv(catalina_base: str, app: str, java_home: str, catalina_home: str,
                 java_opts: List[str], catalina_opts: List[str]) -> str:
    ctx = {
        "app": app,
        "java_home": java_home,
        "catalina_home": catalina_home,
        "catalina_base": catalina_base,
        "java_opts": " ".join(java_opts),
        "catalina_opts": " ".join(catalina_opts),
    }
    path = os.path.join(catalina_base, "bin", "setenv.sh")
    fs.atomic_write(path, templating.render_file("setenv.sh.tmpl", ctx), mode=0o750)
    return path
