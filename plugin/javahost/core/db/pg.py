# coding: utf-8
"""PostgreSQL connection helper — supports any server version 9.4 .. 18."""
from __future__ import annotations

from typing import Dict, Optional

from . import _base
from ..util import validate


class PostgresEngine(_base.Engine):
    name = "postgresql"
    label = "PostgreSQL"
    default_port = 5432
    prefixes = ("postgresql", "postgres", "pg")
    driver_class = "org.postgresql.Driver"
    driver_modern = "org.postgresql:postgresql:42.7.4"   # Java 8+, PG 8.2 .. latest
    driver_legacy = "org.postgresql:postgresql:42.2.29"  # ancient JVMs (Java 6/7)
    detect_cmds = (("pg_config", "--version"), ("psql", "--version"))
    versions = ["9.4", "9.5", "9.6", "10", "11", "12", "13", "14", "15", "16", "17", "18"]

    def build_url(self, host, port, db, *, ssl=True, params=None):
        db = validate.identifier(db, "database")
        query = (["sslmode=require"] if ssl else []) + \
                ["%s=%s" % (k, v) for k, v in _base.safe_params(params)]
        url = "jdbc:postgresql://%s:%d/%s" % (_base.safe_host(host), port, db)
        return url + ("?" + "&".join(query) if query else "")


ENGINE = PostgresEngine()

# --- backward-compatible module API ---
def supported(): return ENGINE.supported()
def normalize_version(v): return ENGINE.normalize(v)
def recommend_driver(java_major=17): return ENGINE.recommend_driver(java_major)
def detect_local(): return ENGINE.detect_local()
def guidance(version=None, java_major=17): return ENGINE.guidance(version, java_major)
def jdbc_url(host, port, db, *, ssl=True, version=None, params=None):
    if version is not None:
        ENGINE.normalize(version)
    return ENGINE.build_url(host, validate.port(port), db, ssl=ssl, params=params)
def render_env(app=None, *, host, port, db, user, password, ssl=True,
               version=None, java_major=17) -> Dict[str, str]:
    return ENGINE.render_env(host=host, port=port, db=db, user=user,
                             password=password, ssl=ssl, version=version, java_major=java_major)
def write_env(catalina_base, mapping): return _base.write_app_env(catalina_base, mapping)
JDBC_DRIVER = PostgresEngine.driver_class
PG_DEFAULT_PORT = PostgresEngine.default_port
PG_VERSIONS = PostgresEngine.versions
