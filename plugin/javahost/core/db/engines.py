# coding: utf-8
"""Registry of supported database engines for Java-app connectivity."""
from __future__ import annotations

from typing import Dict, List

from . import _base, pg, mysql, mongo

# name/alias -> Engine instance
ENGINES: Dict[str, _base.Engine] = {
    "postgresql": pg.ENGINE,
    "postgres": pg.ENGINE,
    "pg": pg.ENGINE,
    "mysql": mysql.MYSQL,
    "mariadb": mysql.MARIADB,
    "mongodb": mongo.ENGINE,
    "mongo": mongo.ENGINE,
}


def get(name: str) -> _base.Engine:
    key = str(name or "").strip().lower()
    if key not in ENGINES:
        raise ValueError("unsupported database engine: %r (supported: %s)"
                         % (name, ", ".join(sorted({e.name for e in ENGINES.values()}))))
    return ENGINES[key]


def write_app_env(catalina_base: str, mapping: Dict[str, str]) -> str:
    return _base.write_app_env(catalina_base, mapping)


def support_matrix() -> List[dict]:
    """UI-friendly summary of every engine, its versions and driver."""
    seen = {}
    for e in ENGINES.values():
        seen[e.name] = {
            "engine": e.name,
            "label": e.label,
            "default_port": e.default_port,
            "versions": e.supported(),
            "driver": e.recommend_driver(),
            "driver_class": e.driver_class,
            "local_detected": e.detect_local(),
        }
    return list(seen.values())
