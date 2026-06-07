# coding: utf-8
"""
Scheduled backups via a single managed cron.d file.

The source of truth is schedules.json (per-app {cron, remote, keep}); the cron.d
file /etc/cron.d/javahost-backups is regenerated from it on every change (robust —
no fragile cron-line parsing). Each entry runs core/backup/run.py for one app.

Writes to /etc/cron.d transparently lift+relock the immutable bit if the dir is
hardened (chattr +i), reusing util.immutable like the systemd unit writer. cron.d
files are picked up automatically — no reload needed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List

from .. import config
from ..util import fs, immutable, validate

SCHEDULES_PATH = "/www/server/javahost/schedules.json"
CRON_PATH = "/etc/cron.d/javahost-backups"
CRON_LOG = "/www/server/javahost/logs/backup-cron.log"
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RUNNER = os.path.join(_PLUGIN_DIR, "core", "backup", "run.py")

# 5-field cron expression of digits / * , - / only (no command injection surface).
_CRON_FIELD = re.compile(r"^[0-9*/,\-]+$")


def validate_cron(expr: str) -> str:
    expr = (expr or "").strip()
    fields = expr.split()
    if len(fields) != 5 or not all(_CRON_FIELD.match(f) for f in fields):
        raise ValueError("invalid cron expression (need 5 fields of [0-9*/,-]): %r" % expr)
    return " ".join(fields)


def _read() -> Dict[str, Dict]:
    try:
        with open(SCHEDULES_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(data: Dict[str, Dict]) -> None:
    fs.ensure_dir(os.path.dirname(SCHEDULES_PATH))
    fs.atomic_write(SCHEDULES_PATH, json.dumps(data, indent=2) + "\n", mode=0o644)


def _norm_remotes(s: Dict) -> List[str]:
    """Destination ids for a schedule entry; legacy `remote:true` → ['all']."""
    r = s.get("remotes")
    if r is not None:
        return list(r)
    return ["all"] if s.get("remote") else []


def _render_cron(data: Dict[str, Dict]) -> str:
    py = sys.executable or "python3"
    lines = ["# Managed by JavaHost — scheduled app backups. Do not edit by hand.",
             "SHELL=/bin/sh", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", ""]
    for app in sorted(data):
        s = data[app]
        cmd = "%s %s --app %s" % (py, _RUNNER, app)
        remotes = _norm_remotes(s)
        if remotes:
            cmd += " --remotes %s" % ",".join(remotes)
        keep = int(s.get("keep") or 0)
        if keep > 0:
            cmd += " --keep %d" % keep
        cmd += " >> %s 2>&1" % CRON_LOG
        lines.append("%s root %s" % (s["cron"], cmd))
    return "\n".join(lines) + "\n"


def _regenerate(data: Dict[str, Dict]) -> None:
    """Write (or remove) the cron.d file from the schedule set, hardening-aware."""
    mh = bool(config.get("manage_hardening", True))
    parent = os.path.dirname(CRON_PATH)
    if not os.path.isdir(parent):
        # no cron.d on this host: schedules.json is still recorded, just inert
        return
    fs.ensure_dir(os.path.dirname(CRON_LOG))
    with immutable.writable(parent, enabled=mh):
        if not data:
            if os.path.exists(CRON_PATH):
                with immutable.writable(CRON_PATH, enabled=mh):
                    os.unlink(CRON_PATH)
            return
        body = _render_cron(data)
        if os.path.exists(CRON_PATH):
            with immutable.writable(CRON_PATH, enabled=mh):
                fs.atomic_write(CRON_PATH, body, mode=0o644)
        else:
            fs.atomic_write(CRON_PATH, body, mode=0o644)


def list_schedules() -> List[Dict]:
    data = _read()
    return [{"app": a, "cron": data[a].get("cron"),
             "remotes": _norm_remotes(data[a]),
             "keep": int(data[a].get("keep") or 0)} for a in sorted(data)]


def set_schedule(app: str, cron_expr: str, remotes=None, keep: int = 7) -> Dict:
    app = validate.identifier(app, "app")
    cron = validate_cron(cron_expr)
    keep = max(0, int(keep))
    if isinstance(remotes, str):
        remotes = [x.strip() for x in remotes.split(",") if x.strip()]
    remotes = list(remotes or [])
    data = _read()
    data[app] = {"cron": cron, "remotes": remotes, "keep": keep}
    _write(data)
    _regenerate(data)
    return {"app": app, "cron": cron, "remotes": remotes, "keep": keep}


def detach_remote(pid: str) -> None:
    """Remove a storage-profile id from every schedule (called when a profile is
    force-deleted) and regenerate the cron file."""
    data = _read()
    changed = False
    for s in data.values():
        r = s.get("remotes")
        if r and pid in r:
            s["remotes"] = [x for x in r if x != pid]
            changed = True
    if changed:
        _write(data)
        _regenerate(data)


def remove_schedule(app: str) -> Dict:
    app = validate.identifier(app, "app")
    data = _read()
    existed = app in data
    if existed:
        del data[app]
        _write(data)
        _regenerate(data)
    return {"app": app, "removed": existed}
