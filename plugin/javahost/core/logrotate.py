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
import gzip
import os
import re
import secrets
import shutil
import stat
import sys
import time
from typing import Dict, Iterator, List, Tuple

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
#
# This runs as root over directories the app user owns (instances/<app>/logs is
# chowned to the service user), so nothing here may follow a symlink: a planted
# link would otherwise have root gzip, truncate, overwrite or delete a file of
# the app's choosing. Every log dir is opened O_NOFOLLOW and every file in it is
# reached relative to that descriptor, never by path.
# --------------------------------------------------------------------------- #
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _log_dirs() -> Iterator[Tuple[str, int]]:
    """Yield (path, dir_fd) for each app's logs/ plus the plugin's own logs dir."""
    paths: List[str] = []
    if os.path.isdir(INSTANCE_ROOT):
        paths += [os.path.join(INSTANCE_ROOT, n, "logs") for n in sorted(os.listdir(INSTANCE_ROOT))]
    paths.append(LOGS_DIR)
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW)
        except OSError:
            continue        # missing, not a directory, or a symlink
        try:
            yield path, fd
        finally:
            os.close(fd)


def _is_plain_file(st: os.stat_result) -> bool:
    # single-link regular file: not a symlink, device, fifo, or a hardlink to
    # some other file
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _entries(dir_fd: int, rotated: bool) -> Iterator[Tuple[str, os.stat_result]]:
    """(name, lstat) of live logs (catalina.out, *.log) or, with `rotated`, of
    their *.gz rotations. Anything that is not a plain file is skipped."""
    for name in sorted(os.listdir(dir_fd)):
        if rotated != name.endswith(".gz"):
            continue
        if not rotated and not (name == "catalina.out" or name.endswith(".log")):
            continue
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if _is_plain_file(st):
            yield name, st


def _unlink(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# rotation (copy-truncate + gzip, keep N)
# --------------------------------------------------------------------------- #
def _rotate_one(dir_fd: int, name: str, keep: int) -> bool:
    """Gzip `name` to `name.1.gz`, shifting older rotations up to `keep`, then
    truncate the live file in place. Returns True if rotated."""
    fd = os.open(name, os.O_RDWR | _NOFOLLOW, dir_fd=dir_fd)
    try:
        if not _is_plain_file(os.fstat(fd)):
            return False    # swapped since it was listed
        if keep > 0:
            # drop the oldest, then shift 1..keep-1 up by one
            _unlink(dir_fd, "%s.%d.gz" % (name, keep))
            for i in range(keep - 1, 0, -1):
                try:
                    os.rename("%s.%d.gz" % (name, i), "%s.%d.gz" % (name, i + 1),
                              src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except OSError:
                    pass
            # gzip current contents into a fresh, unpredictable temp, then publish
            tmp = ".%s.%s.tmp" % (name, secrets.token_hex(8))
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o640,
                          dir_fd=dir_fd)
            try:
                try:
                    owner = os.fstat(dir_fd)       # keep it readable by the app user
                    os.fchown(tfd, owner.st_uid, owner.st_gid)
                except OSError:
                    pass
                with os.fdopen(tfd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as dst, \
                        os.fdopen(os.dup(fd), "rb") as src:
                    shutil.copyfileobj(src, dst, length=1024 * 256)
                os.rename(tmp, "%s.1.gz" % name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                _unlink(dir_fd, tmp)
                raise
        # truncate the live file (writer's append fd keeps the inode); with
        # keep <= 0 no rotation is kept, the space is just reclaimed
        os.ftruncate(fd, 0)
        return True
    finally:
        os.close(fd)


def rotate(max_mb: int = None, keep: int = None) -> Dict:
    """Rotate every managed log that exceeds `max_mb`. Returns a summary."""
    if max_mb is None:
        max_mb = config.log_rotate_max_mb()
    if keep is None:
        keep = config.log_rotate_keep()
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    rotated: List[str] = []
    for path, dir_fd in _log_dirs():
        for name, st in _entries(dir_fd, rotated=False):
            if st.st_size <= max_bytes:
                continue
            try:
                if _rotate_one(dir_fd, name, int(keep)):
                    rotated.append(os.path.join(path, name))
            except OSError:
                continue  # a log vanished or was swapped mid-run — skip
    return {"rotated": rotated, "count": len(rotated), "max_mb": int(max_mb), "keep": int(keep)}


# --------------------------------------------------------------------------- #
# purge (delete rotated *.gz older than retention)
# --------------------------------------------------------------------------- #
_TEMP_RE = re.compile(r"^\..+\.[0-9a-f]{16}\.tmp$")


def _sweep_stale_temps(dir_fd: int, older_than: float = 3600.0) -> None:
    """Remove rotation temp files left by a run that was killed mid-gzip."""
    now = time.time()
    for name in os.listdir(dir_fd):
        if not _TEMP_RE.match(name):
            continue
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if _is_plain_file(st) and now - st.st_mtime > older_than:
            _unlink(dir_fd, name)


def purge(days: int = None) -> Dict:
    """Delete rotated `*.gz` older than `days`. Never touches a live log.
    `days <= 0` means purge ALL rotated artifacts (manual 'purge now')."""
    if days is None:
        days = config.log_purge_days()
    days = int(days)
    cutoff = time.time() - days * 86400 if days > 0 else None
    removed, freed = 0, 0
    for _path, dir_fd in _log_dirs():
        _sweep_stale_temps(dir_fd)
        for name, st in _entries(dir_fd, rotated=True):
            if cutoff is not None and st.st_mtime >= cutoff:
                continue
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                continue
            removed += 1
            freed += st.st_size
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
    live = rotated_bytes = rotated_files = 0
    for _path, dir_fd in _log_dirs():
        live += sum(st.st_size for _n, st in _entries(dir_fd, rotated=False))
        for _n, st in _entries(dir_fd, rotated=True):
            rotated_bytes += st.st_size
            rotated_files += 1
    return {
        "enabled": config.log_rotate_enabled(),
        "when": config.log_rotate_when(),
        "keep": config.log_rotate_keep(),
        "max_mb": config.log_rotate_max_mb(),
        "purge_days": config.log_purge_days(),
        "live_bytes": live,
        "rotated_bytes": rotated_bytes,
        "rotated_files": rotated_files,
        "cron_installed": os.path.exists(CRON_PATH),
    }


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
