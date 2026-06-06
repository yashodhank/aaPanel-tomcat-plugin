# coding: utf-8
"""
PostgreSQL 17 Java-app deployment guidance (not a DB manager).

Provides a JDBC connection-string template and an app env-file generator that
keeps credentials OUT of the WAR/source and out of logs (secret-safe pattern).
The env file is written 0640 root:www and referenced by systemd EnvironmentFile.
"""
from __future__ import annotations

import os
from typing import Dict

from ..util import fs, validate

# Current PostgreSQL JDBC driver coordinates (update as releases land).
JDBC_DRIVER = "org.postgresql.Driver"
JDBC_MAVEN = "org.postgresql:postgresql:42.7.4"


def jdbc_url(host: str, port: int, db: str, *, ssl: bool = True) -> str:
    port = validate.port(port)
    # host/db are not shell-bound; keep them simple to avoid URL injection.
    host = "".join(c for c in str(host) if c.isalnum() or c in ".-_")
    db = validate.identifier(db, "database")
    base = "jdbc:postgresql://%s:%d/%s" % (host, port, db)
    return base + ("?sslmode=require" if ssl else "")


def render_env(app: str, *, host: str, port: int, db: str, user: str,
               password: str, ssl: bool = True) -> Dict[str, str]:
    """Return the env mapping for an app (NOT written here; see write_env)."""
    return {
        "DB_URL": jdbc_url(host, port, db, ssl=ssl),
        "DB_USER": user,
        "DB_PASSWORD": password,   # value never logged by the plugin
        "DB_DRIVER": JDBC_DRIVER,
    }


def write_env(catalina_base: str, mapping: Dict[str, str]) -> str:
    """Write CATALINA_BASE/bin/app.env (0640). Values are shell-quoted."""
    lines = []
    for k, v in mapping.items():
        safe = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
        lines.append('%s="%s"' % (k, safe))
    path = os.path.join(catalina_base, "bin", "app.env")
    fs.atomic_write(path, "\n".join(lines) + "\n", mode=0o640)
    return path


def guidance() -> str:
    return (
        "PostgreSQL 17 + Tomcat: place the JDBC driver (%s) in the app or in "
        "CATALINA_HOME/lib; read credentials from environment (DB_URL/DB_USER/"
        "DB_PASSWORD) via the generated app.env — never hardcode them in the WAR "
        "or source. JavaHost stores them 0640 and injects via systemd "
        "EnvironmentFile so they stay out of process listings and logs." % JDBC_MAVEN
    )
