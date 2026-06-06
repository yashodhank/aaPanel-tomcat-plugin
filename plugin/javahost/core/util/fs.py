# coding: utf-8
"""
Filesystem helpers: secure temp files (F7), atomic writes, ownership markers,
and guarded removal that refuses to delete anything outside plugin-managed
roots or anything not carrying our managed-marker (F14/R3).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Iterable

MANAGED_MARKER = ".javahost-managed"

# Absolute roots the plugin is allowed to create/remove things under.
MANAGED_ROOTS = (
    "/www/server/javahost",
    "/etc/systemd/system",      # only our javahost-*.service units (checked separately)
    "/etc/init.d",              # only our javahost* scripts (checked separately)
)


def mkdtemp(prefix: str = "javahost-") -> str:
    """Create a 0700 temp dir owned by the current user (no predictable names)."""
    path = tempfile.mkdtemp(prefix=prefix)
    os.chmod(path, 0o700)
    return path


def atomic_write(path: str, content: str, mode: int = 0o640) -> None:
    """Write file atomically (temp + rename) with explicit permissions."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_dir(path: str, mode: int = 0o755) -> str:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, mode)
    return path


def mark_managed(path: str) -> None:
    ensure_dir(path)
    atomic_write(os.path.join(path, MANAGED_MARKER), "javahost\n", mode=0o644)


def is_managed(path: str) -> bool:
    return os.path.isfile(os.path.join(path, MANAGED_MARKER))


def _under_managed_root(path: str) -> bool:
    rp = os.path.realpath(path)
    return any(rp == r or rp.startswith(r + os.sep) for r in MANAGED_ROOTS)


def safe_rmtree(path: str, *, require_marker: bool = True) -> None:
    """Remove a managed directory. Refuses paths outside MANAGED_ROOTS, refuses
    to follow symlinks out, and (by default) refuses dirs without the marker."""
    if not path or path in ("/", ""):
        raise ValueError("refusing to remove %r" % path)
    rp = os.path.realpath(path)
    if not _under_managed_root(rp):
        raise ValueError("refusing to remove path outside managed roots: %s" % rp)
    if require_marker and os.path.isdir(rp) and not is_managed(rp):
        raise ValueError("refusing to remove unmanaged dir (no marker): %s" % rp)
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(rp):
        shutil.rmtree(rp)
    elif os.path.exists(rp):
        os.unlink(rp)


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def require_free(path: str, need_bytes: int) -> None:
    """Disk precheck (F12). Raises if not enough free space."""
    have = free_bytes(path if os.path.exists(path) else os.path.dirname(path) or "/")
    if have < need_bytes:
        raise RuntimeError(
            "insufficient disk space at %s: need %d MB, have %d MB"
            % (path, need_bytes // (1 << 20), have // (1 << 20))
        )


def list_managed(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        return []
    return [
        os.path.join(root, d)
        for d in os.listdir(root)
        if is_managed(os.path.join(root, d))
    ]
