# coding: utf-8
"""
Log rotation + purge for JavaHost (stdlib only).

WHY: per-app Tomcat/JAR logs (catalina.out + logs/*.log) and the plugin's own
cron logs grow without bound — a chatty app can fill the disk. JavaHost rotates
them itself (no dependency on the system `logrotate`, which may be absent or
differently configured on a hardened box) and purges old rotations.

ROTATION is **copy-truncate**: the oversized live file is gzipped to `<name>.1.gz`
then truncated in place, so the writer's append fd (catalina.out is opened with
`>>`) keeps writing to the same inode — no restart needed. Older rotations shift
`<name>.1.gz → <name>.2.gz …` up to `keep`, dropping the oldest.

PURGE deletes rotated `*.gz` older than the retention window. A *live* log is
never deleted — only its gzipped rotations.

SCHEDULE: a single managed /etc/cron.d/javahost-logrotate runs `--run` on the
configured cadence, lifting/relocking the immutable bit if the dir is hardened
(same pattern as scheduled backups).
"""
from __future__ import annotations

import argparse
import glob
import gzip
import os
import shutil
import sys
import time
from typing import Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from core import config                       # noqa: E402
from core.util import fs, immutable           # noqa: E402

ROOT = "/www/server/javahost"
INSTANCE_ROOT = ROOT + "/instances"
LOGS_DIR = ROOT + "/logs"                     # plugin-level logs (cron output etc.)
CRON_PATH = "/etc/cron.d/javahost-logrotate"
CRON_LOG = LOGS_DIR + "/logrotate-cron.log"
_RUNNER = os.path.join(_PLUGIN_DIR, "core", "logrotate.py")

_WHEN_CRON = {
    "daily": "0 0 * * *",
    "weekly": "0 0 * * 0",
    "monthly": "0 0 1 * *",
}


# --------------------------------------------------------------------------- #
# target discovery
# --------------------------------------------------------------------------- #
def _targets() -> List[str]:
    """Live log files JavaHost manages: every app's catalina.out + logs/*.log, plus
    the plugin's own cron logs. Rotated artifacts (*.gz) are never targets."""
    out: List[str] = []
    if os.path.isdir(INSTANCE_ROOT):
        for name in os.listdir(INSTANCE_ROOT):
            ldir = os.path.join(INSTANCE_ROOT, name, "logs")
            if not os.path.isdir(ldir):
                continue
            for f in os.listdir(ldir):
                if f.endswith(".gz"):
                    continue
                if f == "catalina.out" or f.endswith(".log"):
                    p = os.path.join(ldir, f)
                    if os.path.isfile(p):
                        out.append(p)
    if os.path.isdir(LOGS_DIR):
        for f in os.listdir(LOGS_DIR):
            if f.endswith(".log") and os.path.isfile(os.path.join(LOGS_DIR, f)):
                out.append(os.path.join(LOGS_DIR, f))
    return out


# --------------------------------------------------------------------------- #
# rotation (copy-truncate + gzip, keep N)
# --------------------------------------------------------------------------- #
def _rotate_one(path: str, keep: int) -> bool:
    """Gzip `path` to `path.1.gz`, shifting older rotations up to `keep`, then
    truncate the live file in place. Returns True if rotated."""
    if keep <= 0:
        # no rotations kept: just reclaim the space (discard)
        with open(path, "r+b") as f:
            f.truncate(0)
        return True
    # drop the oldest, then shift 1..keep-1 up by one
    oldest = "%s.%d.gz" % (path, keep)
    if os.path.exists(oldest):
        try:
            os.unlink(oldest)
        except OSError:
            pass
    for i in range(keep - 1, 0, -1):
        src = "%s.%d.gz" % (path, i)
        if os.path.exists(src):
            os.replace(src, "%s.%d.gz" % (path, i + 1))
    # gzip current contents into <path>.1.gz
    tmp = "%s.1.gz.tmp" % path
    with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 256)
    os.replace(tmp, "%s.1.gz" % path)
    # truncate the live file (writer's append fd keeps the inode)
    with open(path, "r+b") as f:
        f.truncate(0)
    return True


def rotate(max_mb: int = None, keep: int = None) -> Dict:
    """Rotate every managed log that exceeds `max_mb`. Returns a summary."""
    if max_mb is None:
        max_mb = config.log_rotate_max_mb()
    if keep is None:
        keep = config.log_rotate_keep()
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    rotated: List[str] = []
    for path in _targets():
        try:
            if os.path.getsize(path) > max_bytes:
                if _rotate_one(path, int(keep)):
                    rotated.append(path)
        except OSError:
            continue  # a log vanished mid-run (app deleted) — skip
    return {"rotated": rotated, "count": len(rotated), "max_mb": int(max_mb), "keep": int(keep)}


# --------------------------------------------------------------------------- #
# purge (delete rotated *.gz older than retention)
# --------------------------------------------------------------------------- #
def _rotated_artifacts() -> List[str]:
    out: List[str] = []
    for base in _gz_dirs():
        out.extend(glob.glob(os.path.join(base, "*.gz")))
    return out


def _gz_dirs() -> List[str]:
    dirs: List[str] = []
    if os.path.isdir(INSTANCE_ROOT):
        for name in os.listdir(INSTANCE_ROOT):
            ldir = os.path.join(INSTANCE_ROOT, name, "logs")
            if os.path.isdir(ldir):
                dirs.append(ldir)
    if os.path.isdir(LOGS_DIR):
        dirs.append(LOGS_DIR)
    return dirs


def purge(days: int = None) -> Dict:
    """Delete rotated `*.gz` older than `days`. Never touches a live log.
    `days <= 0` means purge ALL rotated artifacts (manual 'purge now')."""
    if days is None:
        days = config.log_purge_days()
    days = int(days)
    cutoff = time.time() - days * 86400 if days > 0 else None
    removed, freed = 0, 0
    for gzf in _rotated_artifacts():
        try:
            if cutoff is not None and os.path.getmtime(gzf) >= cutoff:
                continue
            sz = os.path.getsize(gzf)
            os.unlink(gzf)
            removed += 1
            freed += sz
        except OSError:
            continue
    return {"removed": removed, "freed_bytes": freed, "days": days}


# --------------------------------------------------------------------------- #
# managed cron.d (hardening-aware, mirrors core/backup/schedule.py)
# --------------------------------------------------------------------------- #
def _render_cron(when: str) -> str:
    py = sys.executable or "python3"
    expr = _WHEN_CRON.get(when, _WHEN_CRON["daily"])
    return "\n".join([
        "# Managed by JavaHost — log rotation + purge. Do not edit by hand.",
        "SHELL=/bin/sh",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "",
        "%s root %s %s --run >> %s 2>&1" % (expr, py, _RUNNER, CRON_LOG),
    ]) + "\n"


def apply_schedule() -> Dict:
    """(Re)generate the cron.d file from current config — call after SetLogConfig."""
    enabled = config.log_rotate_enabled()
    when = config.log_rotate_when()
    mh = bool(config.get("manage_hardening", True))
    parent = os.path.dirname(CRON_PATH)
    if not os.path.isdir(parent):
        return {"cron": False, "reason": "no /etc/cron.d on this host"}
    fs.ensure_dir(LOGS_DIR)
    with immutable.writable(parent, enabled=mh):
        if not enabled:
            if os.path.exists(CRON_PATH):
                with immutable.writable(CRON_PATH, enabled=mh):
                    os.unlink(CRON_PATH)
            return {"cron": False, "enabled": False}
        body = _render_cron(when)
        if os.path.exists(CRON_PATH):
            with immutable.writable(CRON_PATH, enabled=mh):
                fs.atomic_write(CRON_PATH, body, mode=0o644)
        else:
            fs.atomic_write(CRON_PATH, body, mode=0o644)
    return {"cron": True, "enabled": True, "when": when}


# --------------------------------------------------------------------------- #
# status (for the Settings card)
# --------------------------------------------------------------------------- #
def status() -> Dict:
    live = sum_sizes(_targets())
    rotated_files = _rotated_artifacts()
    return {
        "enabled": config.log_rotate_enabled(),
        "when": config.log_rotate_when(),
        "keep": config.log_rotate_keep(),
        "max_mb": config.log_rotate_max_mb(),
        "purge_days": config.log_purge_days(),
        "live_bytes": live,
        "rotated_bytes": sum_sizes(rotated_files),
        "rotated_files": len(rotated_files),
        "cron_installed": os.path.exists(CRON_PATH),
    }


def sum_sizes(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------- #
# CLI (invoked by cron: `logrotate.py --run`)
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="javahost-logrotate")
    ap.add_argument("--run", action="store_true", help="rotate then purge (cron)")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--purge", action="store_true")
    a = ap.parse_args(argv)
    do_rot = a.run or a.rotate or not (a.rotate or a.purge)
    do_purge = a.run or a.purge or not (a.rotate or a.purge)
    if do_rot:
        r = rotate()
        print("rotate: %d file(s) over %d MB rotated" % (r["count"], r["max_mb"]))
    if do_purge:
        p = purge()
        print("purge: removed %d rotated file(s), freed %d bytes" % (p["removed"], p["freed_bytes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
