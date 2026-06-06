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
import time
from typing import Dict, List, Optional

from . import templating
from ..util import shell, fs, immutable
from .. import config

SYSTEMD_DIR = "/etc/systemd/system"
INITD_DIR = "/etc/init.d"


def _manage() -> bool:
    return bool(config.get("manage_hardening", True))


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


def _write_unit_file(path: str, content: str, mode: int) -> None:
    """Write a service unit, transparently handling an immutable (hardened) target.

    Creating a file in an immutable directory requires briefly lifting the bit on
    that directory; we re-lock it immediately afterwards (see util.immutable)."""
    parent = os.path.dirname(path)
    mh = _manage()
    with immutable.writable(parent, enabled=mh):
        if os.path.exists(path):
            with immutable.writable(path, enabled=mh):
                fs.atomic_write(path, content, mode)
        else:
            fs.atomic_write(path, content, mode)


def _locked_msg() -> str:
    return ("cannot install a service: %s and %s are immutable (chattr +i — e.g. "
            "aaPanel 'System Hardening') and managed handling is disabled "
            "(manage_hardening=false) or chattr is unavailable. Enable "
            "manage_hardening, disable hardening, or lift the lock, then retry."
            % (SYSTEMD_DIR, INITD_DIR))


def can_manage(d: str) -> bool:
    """Can we install a unit into dir `d`? True if writable, or if it's immutable
    but we're allowed (and able) to lift+relock it."""
    if _can_write(d):
        return True
    return _manage() and immutable.is_immutable(d) and immutable.chattr_available()


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
    if have_systemd() and can_manage(SYSTEMD_DIR):
        path = _unit_path(app)
        _write_unit_file(path, templating.render_file("systemd.service.tmpl", ctx), 0o644)
        shell.run(["systemctl", "daemon-reload"])
        return path
    if can_manage(INITD_DIR):
        path = _script_path(app)
        _write_unit_file(path, templating.render_file("initd.sh.tmpl", ctx), 0o755)
        return path
    raise RuntimeError(_locked_msg())


def install_jar_unit(app: str, java_home: str, app_dir: str, port: int,
                     java_opts: str = "", user: str = "www") -> str:
    """Install a service that runs an executable JAR (`java -jar`). Same locked-dir
    resilience and per-app backend model as install_unit()."""
    ctx = {"app": app, "user": user, "group": user, "java_home": java_home,
           "app_dir": app_dir, "port": str(port), "java_opts": java_opts}
    if have_systemd() and can_manage(SYSTEMD_DIR):
        path = _unit_path(app)
        _write_unit_file(path, templating.render_file("systemd-jar.service.tmpl", ctx), 0o644)
        shell.run(["systemctl", "daemon-reload"])
        return path
    if can_manage(INITD_DIR):
        path = _script_path(app)
        _write_unit_file(path, templating.render_file("initd-jar.sh.tmpl", ctx), 0o755)
        return path
    raise RuntimeError(_locked_msg())


def _verify_systemd_started(app: str) -> None:
    """After enabling, confirm the service didn't get blocked. aaPanel's daemon/
    process protection blocks new services from EXECUTING (status 203/EXEC,
    'Tips from BT security'); we surface that clearly rather than bypass it —
    bypassing anti-persistence exec filtering is out of scope by design."""
    time.sleep(2)
    rc, out, _ = shell.run(["systemctl", "is-active", "javahost-%s.service" % app], check=False)
    if out.strip() == "active":
        return
    _, j, _ = shell.run(["journalctl", "-u", "javahost-%s.service" % app,
                         "--no-pager", "-n", "30"], check=False)
    if any(s in j for s in ("203/EXEC", "BT security", "Tips from BT")):
        raise RuntimeError(
            "service installed but aaPanel process/daemon protection blocked it from "
            "executing (status 203/EXEC, 'Tips from BT security'). This is an "
            "anti-persistence control JavaHost will NOT bypass. Allow it in aaPanel "
            "Security -> daemon/process protection (whitelist 'javahost-*' / "
            "/www/server/javahost), or disable that control, then Repair the app.")


def enable_start(app: str) -> None:
    b = _backend(app)
    if b == "systemd":
        shell.run(["systemctl", "enable", "--now", "javahost-%s.service" % app], check=False)
        _verify_systemd_started(app)
    elif b == "initd":
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
    mh = _manage()
    if b == "systemd":
        unit = "javahost-%s.service" % app
        shell.run(["systemctl", "disable", "--now", unit], check=False)
        # removing a file in an immutable dir needs the bit lifted briefly
        with immutable.writable(SYSTEMD_DIR, enabled=mh):
            if os.path.exists(_unit_path(app)):
                os.unlink(_unit_path(app))
        shell.run(["systemctl", "daemon-reload"], check=False)
    elif b == "initd":
        shell.run([_script_path(app), "stop"], check=False)
        with immutable.writable(INITD_DIR, enabled=mh):
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
