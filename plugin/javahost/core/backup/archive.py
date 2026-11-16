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
import stat
import tarfile
from typing import Iterable, List, Optional, Tuple

# Re-use the same exception name/contract as deploy/war.py for consistency.
from .. import config
from ..deploy.war import UnsafeArchive

_CHUNK = 1 << 16


class UnrestorableBackup(RuntimeError):
    """The app is too large (files, bytes) for an archive restore would accept."""

# Resource ceilings for backup and restore archives. These are the defaults; an
# operator with unusually large applications raises them in Settings (see _lim).
# Backup and restore read the same values, so a backup that is written can
# always be restored on the same host.
MAX_COMPRESSED_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MIN_FREE_SPACE_BYTES = 128 * 1024 * 1024

# constant -> (config key, unit). The setting is stored in the unit shown in the UI.
_SETTINGS = {
    "MAX_ARCHIVE_MEMBERS": ("backup_max_files", 1),
    "MAX_MEMBER_BYTES": ("backup_max_file_gb", 1 << 30),
    "MAX_TOTAL_EXTRACTED_BYTES": ("backup_max_total_gb", 1 << 30),
    "MAX_COMPRESSED_ARCHIVE_BYTES": ("backup_max_archive_gb", 1 << 30),
}


def _lim(name: str) -> int:
    """The limit in force: the operator's setting if one is saved, else the default."""
    key, unit = _SETTINGS[name]
    stored = config.backup_limit(key)
    return stored * unit if stored else globals()[name]


def limits() -> dict:
    """Limits in force and their defaults, in Settings units (count / whole GB)."""
    out = {}
    for name, (key, unit) in _SETTINGS.items():
        short = key[len("backup_"):]
        out[short] = -(-_lim(name) // unit)
        out["default_" + short] = -(-globals()[name] // unit)
    return out


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


def _restorable_name(name: str) -> bool:
    """Whether safe_extract_tar would take a member with this name (the same
    rules as _safe_target, minus the realpath containment it adds)."""
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        return False
    return ".." not in name.replace("\\", "/").split("/")


class _Exactly(object):
    """Reads exactly `size` bytes from a file for tarfile.addfile. A file that
    shrank after it was measured is padded with zeros (the member header is
    already written by then) and reported through `on_short`."""

    def __init__(self, f, size: int, on_short):
        self._f, self._left, self._on_short, self._short = f, size, on_short, False

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        want = self._left if n is None or n < 0 else min(n, self._left)
        data = self._f.read(want)
        if len(data) < want:
            if not self._short:
                self._short = True
                self._on_short()
            data += b"\0" * (want - len(data))
        self._left -= len(data)
        return data


def pack(members: Iterable[Tuple[str, str]], dest_targz: str,
         skipped: Optional[List[str]] = None,
         changed: Optional[List[str]] = None) -> str:
    """Write a gzip tarball at <dest_targz> from (src_path, arcname) pairs.

    The caller controls what is included (exclusion of logs/work/temp etc. is the
    caller's job). Missing sources are skipped.

    The sources are an app's own files, in a tree its user can write while this
    runs as root. So nothing is reached by path after it has been checked: each
    directory is walked through a descriptor, each file is opened O_NOFOLLOW and
    then judged by fstat, and only regular files and directories are stored. A
    link, fifo, socket or device is left out, as is a file that vanished or a
    name restore would refuse; their archive names go to `skipped`. A file that
    shrank while being read is stored padded and named in `changed`.

    What comes out is always something safe_extract_tar will take back: the
    restore-side limits on member count and sizes are enforced here, so an
    oversized app fails its backup now, not its restore later.

    The file is created 0600 and never has a wider mode, since it carries the
    app's DB credentials. Published atomically (temp + rename)."""
    os.makedirs(os.path.dirname(dest_targz), exist_ok=True)
    tmp = dest_targz + ".tmp"
    count = [0]
    total = [0]
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)   # never block on a fifo

    def skip(arcname: str) -> None:
        if skipped is not None:
            skipped.append(arcname)

    def account(ti: tarfile.TarInfo) -> None:
        count[0] += 1
        if ti.isreg():
            total[0] += ti.size
            if ti.size > _lim("MAX_MEMBER_BYTES"):
                raise UnrestorableBackup("%s is %d MB; restore accepts at most %d MB per file"
                                         % (ti.name, ti.size >> 20, _lim("MAX_MEMBER_BYTES") >> 20))
        if count[0] > _lim("MAX_ARCHIVE_MEMBERS"):
            raise UnrestorableBackup("more than %d files; restore would refuse this archive"
                                     % _lim("MAX_ARCHIVE_MEMBERS"))
        if total[0] > _lim("MAX_TOTAL_EXTRACTED_BYTES"):
            raise UnrestorableBackup("more than %d MB of data; restore would refuse this archive"
                                     % (_lim("MAX_TOTAL_EXTRACTED_BYTES") >> 20))

    def info(arcname: str, st: os.stat_result, kind: bytes) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(arcname)
        ti.type = kind
        ti.mode = stat.S_IMODE(st.st_mode)
        ti.mtime = int(st.st_mtime)
        ti.size = st.st_size if kind == tarfile.REGTYPE else 0
        return ti

    def add_file(tf: tarfile.TarFile, dir_fd: int, name: str, arcname: str) -> None:
        if not _restorable_name(arcname):
            return skip(arcname)
        try:
            fd = os.open(name, file_flags, dir_fd=dir_fd)
        except OSError:
            return skip(arcname)                 # a link, or gone since it was listed
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return skip(arcname)
            ti = info(arcname, st, tarfile.REGTYPE)
            account(ti)
            tf.addfile(ti, _Exactly(f, ti.size, lambda: changed is not None and changed.append(arcname)))

    def add_dir(tf: tarfile.TarFile, src: str, arc: str) -> None:
        # fwalk hands back a descriptor per directory and will not descend
        # through a symlink (it compares lstat with what it actually opened)
        for root, dirs, files, dir_fd in os.fwalk(src, follow_symlinks=False):
            rel = os.path.relpath(root, src)
            arcdir = arc if rel == "." else "%s/%s" % (arc, rel.replace(os.sep, "/"))
            if not _restorable_name(arcdir):
                skip(arcdir)
                dirs[:] = []
                continue
            ti = info(arcdir, os.fstat(dir_fd), tarfile.DIRTYPE)
            account(ti)
            tf.addfile(ti)
            for d in list(dirs):
                try:
                    is_link = stat.S_ISLNK(os.stat(d, dir_fd=dir_fd, follow_symlinks=False).st_mode)
                except OSError:
                    is_link = True
                if is_link:                      # listed as a directory, never walked
                    dirs.remove(d)
                    skip("%s/%s" % (arcdir, d))
            dirs.sort()
            for name in sorted(files):
                add_file(tf, dir_fd, name, "%s/%s" % (arcdir, name))

    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        with os.fdopen(fd, "wb") as raw:
            os.fchmod(raw.fileno(), 0o600)       # O_CREAT mode is masked by umask
            with tarfile.open(fileobj=raw, mode="w:gz") as tf:
                for src, arc in members:
                    if not src:
                        continue
                    try:
                        st = os.lstat(src)
                    except OSError:
                        continue                 # missing source
                    if stat.S_ISDIR(st.st_mode):
                        add_dir(tf, src, arc)
                    elif stat.S_ISREG(st.st_mode):
                        parent = os.open(os.path.dirname(src) or ".",
                                         os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                        try:
                            add_file(tf, parent, os.path.basename(src), arc)
                        finally:
                            os.close(parent)
                    else:
                        skip(arc)
        if os.path.getsize(tmp) > _lim("MAX_COMPRESSED_ARCHIVE_BYTES"):
            raise UnrestorableBackup("archive is larger than %d MB; restore would refuse it"
                                     % (_lim("MAX_COMPRESSED_ARCHIVE_BYTES") >> 20))
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
    if compressed_size > _lim("MAX_COMPRESSED_ARCHIVE_BYTES"):
        raise UnsafeArchive(
            "compressed archive too large: %d bytes (limit %d)"
            % (compressed_size, _lim("MAX_COMPRESSED_ARCHIVE_BYTES"))
        )


def _validate_member(base_real: str, member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        raise UnsafeArchive("link entry in archive: %r" % member.name)
    if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
        raise UnsafeArchive("device/fifo entry in archive: %r" % member.name)
    _safe_target(base_real, member.name)
    if member.isreg() and member.size > _lim("MAX_MEMBER_BYTES"):
        raise UnsafeArchive(
            "archive member too large: %r is %d bytes (limit %d)"
            % (member.name, member.size, _lim("MAX_MEMBER_BYTES"))
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
            if count > _lim("MAX_ARCHIVE_MEMBERS"):
                raise UnsafeArchive(
                    "archive has too many members (limit %d)" % _lim("MAX_ARCHIVE_MEMBERS")
                )
            _validate_member(base_real, member)
            if member.isreg():
                total_size += member.size
                if total_size > _lim("MAX_TOTAL_EXTRACTED_BYTES"):
                    raise UnsafeArchive(
                        "archive expanded size exceeds %d bytes"
                        % _lim("MAX_TOTAL_EXTRACTED_BYTES")
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
                if extracted_count > _lim("MAX_ARCHIVE_MEMBERS"):
                    raise UnsafeArchive(
                        "archive has too many members (limit %d)" % _lim("MAX_ARCHIVE_MEMBERS")
                    )
                _validate_member(base_real, m)
                if m.isreg():
                    extracted_size += m.size
                    if extracted_size > _lim("MAX_TOTAL_EXTRACTED_BYTES"):
                        raise UnsafeArchive(
                            "archive expanded size exceeds %d bytes"
                            % _lim("MAX_TOTAL_EXTRACTED_BYTES")
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
                    if count > _lim("MAX_ARCHIVE_MEMBERS"):
                        return None
                    if m.isreg():
                        if m.size > _lim("MAX_MEMBER_BYTES"):
                            return None
                        total_size += m.size
                        if total_size > _lim("MAX_TOTAL_EXTRACTED_BYTES"):
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
