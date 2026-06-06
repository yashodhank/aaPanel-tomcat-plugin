# coding: utf-8
"""
Service management (closes B1/F4 — JAVA_HOME comes from env, never parsed from a
shebang). Prefers systemd; falls back to a lint-clean init.d script when systemd
is absent OR its unit dir is not writable.

Resilient to locked service dirs: some panels (e.g. aaPanel "System Hardening")
set the immutable bit (chattr +i) on /etc/systemd/system and /etc/init.d. We
detect that with a real write probe (os.access can't see the immutable attr) and
raise a clear, actionable error instead of a cryptic EPERM. Per-app backend is
resolved by which unit/script actually exists, so a fallback stays consistent.
"""
from __future__ import annotations

import os
import tempfile
from typing import Dict, List, Optional

from . import templating
from ..util import shell, fs

SYSTEMD_DIR = "/etc/systemd/system"
INITD_DIR = "/etc/init.d"


def have_systemd() -> bool:
    return bool(shell.which("systemctl")) and os.path.isdir("/run/systemd/system")


def _can_write(d: str) -> bool:
    """Real write probe — catches the immutable bit, which os.access misses."""
    if not os.path.isdir(d):
        return False
    try:
        fd, p = tempfile.mkstemp(dir=d, prefix=".javahost-probe-")
        os.close(fd)
        os.unlink(p)
        return True
    except OSError:
        return False


def _unit_path(app: str) -> str:
    return os.path.join(SYSTEMD_DIR, "javahost-%s.service" % app)


def _script_path(app: str) -> str:
    return os.path.join(INITD_DIR, "javahost-%s" % app)


def _backend(app: str) -> Optional[str]:
    """Which manager actually owns this app: 'systemd', 'initd', or None."""
    if os.path.exists(_unit_path(app)):
        return "systemd"
    if os.path.exists(_script_path(app)):
        return "initd"
    return None


def _ctx(app, java_home, catalina_home, catalina_base, user="www") -> Dict[str, str]:
    return {"app": app, "user": user, "group": user, "java_home": java_home,
            "catalina_home": catalina_home, "catalina_base": catalina_base}


def install_unit(app: str, java_home: str, catalina_home: str, catalina_base: str,
                 user: str = "www") -> str:
    """Render + install the service unit (systemd preferred, init.d fallback)."""
    ctx = _ctx(app, java_home, catalina_home, catalina_base, user)
    if have_systemd() and _can_write(SYSTEMD_DIR):
        path = _unit_path(app)
        fs.atomic_write(path, templating.render_file("systemd.service.tmpl", ctx), mode=0o644)
        shell.run(["systemctl", "daemon-reload"])
        return path
    if _can_write(INITD_DIR):
        path = _script_path(app)
        fs.atomic_write(path, templating.render_file("initd.sh.tmpl", ctx), mode=0o755)
        return path
    raise RuntimeError(
        "cannot install a service: both %s and %s are not writable "
        "(likely immutable via chattr +i — e.g. aaPanel 'System Hardening'). "
        "Disable system hardening / lift the lock on the service directory, then retry."
        % (SYSTEMD_DIR, INITD_DIR))


def enable_start(app: str) -> None:
    if _backend(app) == "systemd":
        shell.run(["systemctl", "enable", "--now", "javahost-%s.service" % app])
    elif _backend(app) == "initd":
        shell.run([_script_path(app), "start"])
    else:
        raise RuntimeError("no service installed for app: %s" % app)


def action(app: str, what: str) -> None:
    if what not in ("start", "stop", "restart"):
        raise ValueError("bad action: %r" % what)
    b = _backend(app)
    if b == "systemd":
        shell.run(["systemctl", what, "javahost-%s.service" % app])
    elif b == "initd":
        shell.run([_script_path(app), what])
    else:
        raise RuntimeError("no service installed for app: %s" % app)


def status(app: str) -> str:
    b = _backend(app)
    if b == "systemd":
        rc, out, _ = shell.run(["systemctl", "is-active", "javahost-%s.service" % app], check=False)
        return out.strip() or "unknown"
    if b == "initd":
        rc, out, _ = shell.run([_script_path(app), "status"], check=False)
        return "active" if rc == 0 else "inactive"
    return "absent"


def remove_unit(app: str) -> None:
    b = _backend(app)
    if b == "systemd":
        unit = "javahost-%s.service" % app
        shell.run(["systemctl", "disable", "--now", unit], check=False)
        if os.path.exists(_unit_path(app)):
            os.unlink(_unit_path(app))
        shell.run(["systemctl", "daemon-reload"], check=False)
    elif b == "initd":
        shell.run([_script_path(app), "stop"], check=False)
        if os.path.exists(_script_path(app)):
            os.unlink(_script_path(app))


def write_setenv(catalina_base: str, app: str, java_home: str, catalina_home: str,
                 java_opts: List[str], catalina_opts: List[str]) -> str:
    ctx = {"app": app, "java_home": java_home, "catalina_home": catalina_home,
           "catalina_base": catalina_base, "java_opts": " ".join(java_opts),
           "catalina_opts": " ".join(catalina_opts)}
    path = os.path.join(catalina_base, "bin", "setenv.sh")
    fs.atomic_write(path, templating.render_file("setenv.sh.tmpl", ctx), mode=0o750)
    return path
