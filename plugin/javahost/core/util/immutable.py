# coding: utf-8
"""
Safe handling of the ext2/4 immutable bit (chattr +i).

Panels like aaPanel "System Hardening" set the immutable bit on system dirs
(/etc/systemd/system, /etc/init.d) so even root cannot create files there. The
plugin, running as the panel's root, can momentarily lift the bit on a path it
owns, write, and IMMEDIATELY re-apply it — preserving the hardening posture
rather than disabling it. Every lift/restore is logged. No-op (safe) when the
path isn't immutable, when chattr is unavailable, or when disabled by config.
"""
from __future__ import annotations

import contextlib
import sys

from . import shell


def chattr_available() -> bool:
    return bool(shell.which("chattr")) and bool(shell.which("lsattr"))


def is_immutable(path: str) -> bool:
    """True if `path` carries the immutable (i) attribute."""
    if not chattr_available():
        return False
    rc, out, _ = shell.run(["lsattr", "-d", path], check=False)
    if rc != 0 or not out.strip():
        return False
    flags = out.split()[0]
    # 'i' in the attribute field means immutable (no other attr uses 'i').
    return "i" in flags


def _set(flag: str, path: str) -> None:
    shell.run(["chattr", flag, path], check=False)


@contextlib.contextmanager
def writable(path: str, *, enabled: bool = True, relock: bool = True):
    """Context manager: ensure `path` is writable for the duration, then restore.

    If `path` is immutable and `enabled`/chattr allow it, lift the bit and re-apply
    it on exit (so the system stays hardened). Otherwise a pure no-op.
    """
    lifted = False
    if enabled and is_immutable(path):
        sys.stderr.write("[javahost] lifting immutable bit on %s (will re-lock)\n" % path)
        _set("-i", path)
        lifted = True
    try:
        yield lifted
    finally:
        if lifted and relock:
            _set("+i", path)
            sys.stderr.write("[javahost] re-locked immutable bit on %s\n" % path)


def relock(path: str, *, enabled: bool = True) -> None:
    """Apply the immutable bit to a path we just created (match hardening)."""
    if enabled and chattr_available():
        _set("+i", path)
