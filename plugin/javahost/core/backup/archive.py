# coding: utf-8
"""
Hardened tar pack/extract for JavaHost backups & restores.

`safe_extract_tar` is the ONLY untrusted-input boundary in the backup feature
(restore-from-file accepts an operator-supplied .tar.gz). It mirrors the
zip-slip defense in deploy/war.py (realpath-containment per member) AND adds the
tar-specific rejections a zip can't express:

  * symlink / hardlink members (issym/islnk)  -> rejected
  * device / fifo / char / block nodes (isdev) -> rejected
  * absolute names and `..` traversal          -> rejected

We never call TarFile.extract()/extractall() (which honor the member's own path
and link targets); we stream each regular file to a realpath-validated target.
Stdlib only. pack() is the trusted side (our own instance files); it writes the
archive 0600 because backups contain the app's DB credentials (bin/app.env).
"""
from __future__ import annotations

import os
import tarfile
from typing import Iterable, Tuple

# Re-use the same exception name/contract as deploy/war.py for consistency.
from ..deploy.war import UnsafeArchive

_CHUNK = 1 << 16


def _safe_target(base_real: str, name: str) -> str:
    """Resolve <name> under base_real or raise UnsafeArchive. Rejects absolute
    paths, drive letters, `..` segments and any realpath escaping base_real."""
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        raise UnsafeArchive("absolute path in archive: %r" % name)
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise UnsafeArchive("path traversal in archive: %r" % name)
    target = os.path.realpath(os.path.join(base_real, name))
    if target != base_real and not target.startswith(base_real + os.sep):
        raise UnsafeArchive("path traversal in archive: %r" % name)
    return target


def pack(members: Iterable[Tuple[str, str]], dest_targz: str) -> str:
    """Write a gzip tarball at <dest_targz> from (src_path, arcname) pairs.

    The caller controls EXACTLY what is included (exclusion of logs/work/temp etc.
    is the caller's job). Written atomically (temp + rename) with mode 0600 since
    the archive may carry the app's DB credentials. Missing sources are skipped."""
    os.makedirs(os.path.dirname(dest_targz), exist_ok=True)
    tmp = dest_targz + ".tmp"
    try:
        with tarfile.open(tmp, "w:gz") as tf:
            for src, arc in members:
                if src and os.path.exists(src):
                    tf.add(src, arcname=arc, recursive=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest_targz)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest_targz


def safe_extract_tar(targz: str, dest_dir: str) -> str:
    """Safely extract <targz> into <dest_dir>. Never extracts outside dest_dir,
    never materializes a link/device member. Returns dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    base_real = os.path.realpath(dest_dir)
    with tarfile.open(targz, "r:gz") as tf:
        for m in tf.getmembers():
            # Reject link + special members BEFORE any path work.
            if m.issym() or m.islnk():
                raise UnsafeArchive("link entry in archive: %r" % m.name)
            if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise UnsafeArchive("device/fifo entry in archive: %r" % m.name)
            target = _safe_target(base_real, m.name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not m.isreg():
                # Unknown/unsupported member type — skip rather than honor it.
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
            try:
                os.chmod(target, m.mode & 0o777)
            except OSError:
                pass
    return dest_dir


def read_member_bytes(targz: str, arcname: str):
    """Return the bytes of a single member (e.g. manifest.json), or None.
    The member name is matched exactly; no extraction to disk. Defensive."""
    try:
        with tarfile.open(targz, "r:gz") as tf:
            try:
                m = tf.getmember(arcname)
            except KeyError:
                return None
            if not m.isreg():
                return None
            src = tf.extractfile(m)
            return src.read() if src else None
    except Exception:
        return None
