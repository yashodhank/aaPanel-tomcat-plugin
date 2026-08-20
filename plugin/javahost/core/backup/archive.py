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
import shutil
import tarfile
from typing import Iterable, Tuple

# Re-use the same exception name/contract as deploy/war.py for consistency.
from ..deploy.war import UnsafeArchive

_CHUNK = 1 << 16

# Resource ceilings for operator-supplied restore archives.  These are module
# constants on purpose: deployments with unusually large applications can
# override them in their packaged build without weakening the default boundary.
MAX_COMPRESSED_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MIN_FREE_SPACE_BYTES = 128 * 1024 * 1024


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


def _check_compressed_size(source) -> None:
    try:
        if hasattr(source, "fileno"):
            compressed_size = os.fstat(source.fileno()).st_size
        else:
            compressed_size = os.path.getsize(source)
    except OSError as exc:
        raise UnsafeArchive("cannot stat archive: %s" % exc)
    if compressed_size > MAX_COMPRESSED_ARCHIVE_BYTES:
        raise UnsafeArchive(
            "compressed archive too large: %d bytes (limit %d)"
            % (compressed_size, MAX_COMPRESSED_ARCHIVE_BYTES)
        )


def _validate_member(base_real: str, member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        raise UnsafeArchive("link entry in archive: %r" % member.name)
    if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
        raise UnsafeArchive("device/fifo entry in archive: %r" % member.name)
    _safe_target(base_real, member.name)
    if member.isreg() and member.size > MAX_MEMBER_BYTES:
        raise UnsafeArchive(
            "archive member too large: %r is %d bytes (limit %d)"
            % (member.name, member.size, MAX_MEMBER_BYTES)
        )


def _preflight(open_file, base_real: str) -> int:
    """Validate the entire archive before materializing the first member."""
    _check_compressed_size(open_file)
    count = 0
    total_size = 0
    open_file.seek(0)
    with tarfile.open(fileobj=open_file, mode="r:gz") as tf:
        for member in tf:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise UnsafeArchive(
                    "archive has too many members (limit %d)" % MAX_ARCHIVE_MEMBERS
                )
            _validate_member(base_real, member)
            if member.isreg():
                total_size += member.size
                if total_size > MAX_TOTAL_EXTRACTED_BYTES:
                    raise UnsafeArchive(
                        "archive expanded size exceeds %d bytes"
                        % MAX_TOTAL_EXTRACTED_BYTES
                    )
    return total_size


def safe_extract_tar(targz: str, dest_dir: str) -> str:
    """Safely extract <targz> into <dest_dir>. Never extracts outside dest_dir,
    never materializes a link/device member. Returns dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    base_real = os.path.realpath(dest_dir)
    # Keep one descriptor open across preflight and extraction. Replacing the
    # archive pathname cannot swap in different bytes after validation.
    with open(targz, "rb") as open_file:
        total_size = _preflight(open_file, base_real)
        free = shutil.disk_usage(base_real).free
        required = total_size + MIN_FREE_SPACE_BYTES
        if free < required:
            raise UnsafeArchive(
                "insufficient free space for archive: need %d bytes, have %d"
                % (required, free)
            )
        extracted_count = 0
        extracted_size = 0
        open_file.seek(0)
        with tarfile.open(fileobj=open_file, mode="r:gz") as tf:
            for m in tf:
                # Defense in depth if the opened file itself changes in place.
                extracted_count += 1
                if extracted_count > MAX_ARCHIVE_MEMBERS:
                    raise UnsafeArchive(
                        "archive has too many members (limit %d)" % MAX_ARCHIVE_MEMBERS
                    )
                _validate_member(base_real, m)
                if m.isreg():
                    extracted_size += m.size
                    if extracted_size > MAX_TOTAL_EXTRACTED_BYTES:
                        raise UnsafeArchive(
                            "archive expanded size exceeds %d bytes"
                            % MAX_TOTAL_EXTRACTED_BYTES
                        )
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
        with open(targz, "rb") as open_file:
            _check_compressed_size(open_file)
            with tarfile.open(fileobj=open_file, mode="r:gz") as tf:
                total_size = 0
                for count, m in enumerate(tf, start=1):
                    if count > MAX_ARCHIVE_MEMBERS:
                        return None
                    if m.isreg():
                        if m.size > MAX_MEMBER_BYTES:
                            return None
                        total_size += m.size
                        if total_size > MAX_TOTAL_EXTRACTED_BYTES:
                            return None
                    if m.name != arcname:
                        continue
                    if not m.isreg() or m.size > MAX_MANIFEST_BYTES:
                        return None
                    src = tf.extractfile(m)
                    if src is None:
                        return None
                    with src:
                        body = src.read(MAX_MANIFEST_BYTES + 1)
                    return body if len(body) <= MAX_MANIFEST_BYTES else None
                return None
    except Exception:
        return None
