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
import re
from typing import Dict, List, Optional, Tuple

from ..util import fs, validate

_VER_TOKEN = re.compile(r"(\d+(?:\.\d+)?)")
_PARAM_KEY = re.compile(r"^[A-Za-z0-9_]+$")
_PARAM_VAL = re.compile(r"^[A-Za-z0-9_.\-]+$")


def safe_host(host: str) -> str:
    return "".join(c for c in str(host) if c.isalnum() or c in ".-_")


def safe_params(params: Optional[Dict[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, v in (params or {}).items():
        if not _PARAM_KEY.match(str(k)) or not _PARAM_VAL.match(str(v)):
            raise ValueError("unsafe connection parameter: %r=%r" % (k, v))
        out.append((str(k), str(v)))
    return out


def write_app_env(catalina_base: str, mapping: Dict[str, str]) -> str:
    """Write CATALINA_BASE/bin/app.env (0640), values shell-escaped. Secret-safe."""
    lines = []
    for k, v in mapping.items():
        safe = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
        lines.append('%s="%s"' % (k, safe))
    path = os.path.join(catalina_base, "bin", "app.env")
    fs.atomic_write(path, "\n".join(lines) + "\n", mode=0o640)
    return path


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
