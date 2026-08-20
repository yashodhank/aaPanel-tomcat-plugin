# coding: utf-8
"""
Shared database-engine machinery for JavaHost.

JavaHost is a Java/Tomcat manager, not a database manager — this layer only
helps Java apps *connect* to a database: it builds correct connection URLs,
picks the right JDBC/driver artifact for the JVM, and writes credentials to a
secret-safe env file (0640, systemd EnvironmentFile-friendly). Credentials are
never logged.

One `Engine` models each database family; concrete engines live in pg.py,
mysql.py (MySQL + MariaDB), and mongo.py.
"""
from __future__ import annotations

import os
import errno
import re
import secrets
import stat
from typing import Dict, List, Optional, Tuple

from ..util import fs, validate

_VER_TOKEN = re.compile(r"(\d+(?:\.\d+)?)")
_PARAM_KEY = re.compile(r"^[A-Za-z0-9_]+$")
_PARAM_VAL = re.compile(r"^[A-Za-z0-9_.\-]+$")
_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class UnsafeAppEnvError(RuntimeError):
    """The app.env location or file failed privileged-read safety checks."""


def _unsafe_location() -> UnsafeAppEnvError:
    return UnsafeAppEnvError("unsafe app.env location")


def _unsafe_file() -> UnsafeAppEnvError:
    return UnsafeAppEnvError("unsafe app.env")


def _open_managed_bin(catalina_base: str) -> int:
    """Open a managed instance's bin directory without following path symlinks."""
    base = os.path.abspath(os.fspath(catalina_base))
    managed_root = os.path.abspath(fs.MANAGED_ROOTS[0])
    try:
        in_scope = os.path.commonpath((base, managed_root)) == managed_root
    except ValueError:
        in_scope = False
    if not in_scope or base == managed_root:
        raise _unsafe_location()

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise _unsafe_location()
    dir_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | directory | nofollow
    fd = -1
    try:
        fd = os.open(os.path.sep, dir_flags)
        for part in base.split(os.path.sep):
            if not part:
                continue
            next_fd = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd

        marker_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NONBLOCK", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
        marker_fd = os.open(fs.MANAGED_MARKER, marker_flags, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(marker_fd).st_mode):
                raise _unsafe_location()
        finally:
            os.close(marker_fd)

        bin_fd = os.open("bin", dir_flags, dir_fd=fd)
        os.close(fd)
        return bin_fd
    except UnsafeAppEnvError:
        if fd >= 0:
            os.close(fd)
        raise
    except (OSError, ValueError):
        if fd >= 0:
            os.close(fd)
        raise _unsafe_location()


def _read_app_env_fd(bin_fd: int) -> Dict[str, str]:
    env: Dict[str, str] = {}
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open("app.env", flags, dir_fd=bin_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return env
        raise _unsafe_file()

    try:
        file_st = os.fstat(fd)
        bin_st = os.fstat(bin_fd)
        if not stat.S_ISREG(file_st.st_mode):
            raise _unsafe_file()
        if stat.S_IMODE(file_st.st_mode) != 0o640:
            raise _unsafe_file()
        if file_st.st_uid not in (os.geteuid(), bin_st.st_uid):
            raise _unsafe_file()

        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
            fd = -1
            for line in f:
                line = line.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                m = _ENV_LINE.match(line)
                if not m:
                    continue
                key, raw = m.group(1), m.group(2).strip()
                if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
                    decoded = []
                    inner = raw[1:-1]
                    index = 0
                    while index < len(inner):
                        if (inner[index] == "\\" and index + 1 < len(inner)
                                and inner[index + 1] in ('\\', '"')):
                            decoded.append(inner[index + 1])
                            index += 2
                        else:
                            decoded.append(inner[index])
                            index += 1
                    raw = "".join(decoded)
                elif len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
                    raw = raw[1:-1]
                env[key] = raw
    finally:
        if fd >= 0:
            os.close(fd)
    return env


def safe_host(host: str) -> str:
    return "".join(c for c in str(host) if c.isalnum() or c in ".-_")


def safe_params(params: Optional[Dict[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, v in (params or {}).items():
        if not _PARAM_KEY.match(str(k)) or not _PARAM_VAL.match(str(v)):
            raise ValueError("unsafe connection parameter: %r=%r" % (k, v))
        out.append((str(k), str(v)))
    return out


def read_app_env(catalina_base: str) -> Dict[str, str]:
    """Parse CATALINA_BASE/bin/app.env (KEY=val / KEY=\"val\"). Never logs values.

    Refuses symlinks / non-regular files so a compromised ``www``-owned instance
    cannot redirect this privileged read at an arbitrary host path (SetDbEnv
    runs as the panel/root and would otherwise copy foreign KEY=value material
    into a new app.env).
    """
    bin_fd = _open_managed_bin(catalina_base)
    try:
        return _read_app_env_fd(bin_fd)
    finally:
        os.close(bin_fd)


def write_app_env(catalina_base: str, mapping: Dict[str, str]) -> str:
    """Merge mapping into CATALINA_BASE/bin/app.env (0640).

    Preserves non-DB_* keys already on disk (JAR loopback bind, SERVER_PORT,
    JAVA_HOME, Spring profiles). Existing DB_* keys are dropped and replaced by
    ``mapping`` so engine switches do not leave stale credentials. Values are
    shell-escaped. Secret-safe — never log the mapping (may contain DB_PASSWORD).
    """
    path = os.path.join(catalina_base, "bin", "app.env")
    bin_fd = _open_managed_bin(catalina_base)
    tmp_name = ".app.env.tmp-%s" % secrets.token_hex(16)
    try:
        existing = _read_app_env_fd(bin_fd)
        merged: Dict[str, str] = {
            k: v for k, v in existing.items() if not str(k).startswith("DB_")
        }
        merged.update(mapping)
        lines = []
        for k, v in merged.items():
            safe = (str(v).replace("\r", "").replace("\n", "")
                    .replace("\\", "\\\\").replace('"', '\\"'))
            lines.append('%s="%s"' % (k, safe))
        body = "\n".join(lines) + "\n"

        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        tmp_fd = os.open(tmp_name, flags, 0o600, dir_fd=bin_fd)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                tmp_fd = -1
                f.write(body)
                f.flush()
                os.fchmod(f.fileno(), 0o640)
                os.fsync(f.fileno())
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)
        os.replace(tmp_name, "app.env", src_dir_fd=bin_fd, dst_dir_fd=bin_fd)
        os.fsync(bin_fd)
        return path
    finally:
        try:
            os.unlink(tmp_name, dir_fd=bin_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(bin_fd)

class Engine(object):
    """Base connection-helper engine. Subclasses set class attributes and may
    override build_url / env_vars / detect_local."""

    name = "db"
    label = "Database"
    default_port = 0
    versions: List[str] = []
    prefixes: Tuple[str, ...] = ()
    driver_class = ""
    driver_modern = ""     # Maven coords for Java 8+
    driver_legacy = ""     # Maven coords for ancient JVMs (Java 6/7)
    detect_cmds: Tuple[Tuple[str, ...], ...] = ()

    # --- versions ---
    def supported(self) -> List[str]:
        return list(self.versions)

    def normalize(self, value) -> str:
        s = str(value or "").strip().lower()
        for p in self.prefixes:
            s = s.replace(p, "")
        m = _VER_TOKEN.search(s)
        if not m:
            raise ValueError("invalid %s version: %r" % (self.name, value))
        v = m.group(1)
        if v in self.versions:
            return v
        short = ".".join(v.split(".")[:2])     # 8.0.39 -> 8.0
        if short in self.versions:
            return short
        major = v.split(".")[0]                # 17.2 -> 17
        if major in self.versions:
            return major
        raise ValueError(
            "unsupported %s version: %r (supported: %s)"
            % (self.name, value, ", ".join(self.versions))
        )

    # --- driver ---
    def recommend_driver(self, java_major: int = 17) -> str:
        return self.driver_modern if java_major >= 8 else (self.driver_legacy or self.driver_modern)

    # --- url / env (override in subclasses) ---
    def build_url(self, host: str, port: int, db: str, *, ssl: bool = True,
                  params: Optional[Dict[str, str]] = None) -> str:
        raise NotImplementedError

    def env_vars(self, url: str, user: str, password: str, java_major: int) -> Dict[str, str]:
        return {
            "DB_URL": url,
            "DB_USER": user,
            "DB_PASSWORD": password,
            "DB_DRIVER": self.driver_class,
            "DB_DRIVER_MAVEN": self.recommend_driver(java_major),
        }

    def render_env(self, *, host: str, port=None, db: str, user: str, password: str,
                   ssl: bool = True, version: Optional[str] = None,
                   java_major: int = 17, params: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        if version is not None:
            self.normalize(version)
        port = validate.port(port if port not in (None, "", 0) else self.default_port)
        url = self.build_url(host, port, db, ssl=ssl, params=params)
        return self.env_vars(url, user, password, java_major)

    # --- misc ---
    def detect_local(self) -> Optional[str]:
        from ..util import shell
        for cmd in self.detect_cmds:
            path = shell.which(cmd[0])
            if not path:
                continue
            rc, out, err = shell.run([path] + list(cmd[1:]), check=False)
            m = re.search(r"(\d+(?:\.\d+){0,2})", (out or "") + (err or ""))
            if m:
                try:
                    return self.normalize(m.group(1))
                except ValueError:
                    return m.group(1).split(".")[0]
        return None

    def guidance(self, version: Optional[str] = None, java_major: int = 17) -> str:
        rng = ("%s–%s" % (self.versions[0], self.versions[-1])) if self.versions else ""
        ver = ("%s %s" % (self.label, self.normalize(version))) if version else ("%s %s" % (self.label, rng))
        return (
            "%s: add the driver %s to your app (WEB-INF/lib) or CATALINA_HOME/lib. "
            "Read DB_URL / DB_USER / DB_PASSWORD from the environment — JavaHost writes "
            "them to app.env (mode 0640) and systemd injects it via EnvironmentFile, so "
            "credentials stay out of process listings and logs. Never hardcode them in "
            "the WAR or source." % (ver, self.recommend_driver(java_major))
        )
