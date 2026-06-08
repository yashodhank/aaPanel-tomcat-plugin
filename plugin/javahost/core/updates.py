# coding: utf-8
"""
Upstream-update detection for managed runtimes (JDK + Tomcat).

Compares each plugin-managed JDK and installed Tomcat against the latest upstream
version (Adoptium API / Apache index). Results are CACHED with a TTL so opening
the Runtimes tab is instant and doesn't stall on the network; a manual
"Check for updates" passes force=True to bypass the cache.

A live network failure surfaces in `errors` (instead of silently pretending
everything is current); the per-item `latest` is then null and `update` False.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, Optional

from core.runtime import java
from core.tomcat import registry, installer

CACHE_PATH = "/www/server/javahost/runtime_updates_cache.json"
TTL_SECONDS = 24 * 3600


def _read_cache() -> Optional[Dict]:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _write_cache(data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def invalidate() -> None:
    """Drop the cache (call after an update so the badge re-evaluates)."""
    try:
        os.unlink(CACHE_PATH)
    except OSError:
        pass


def _patch_tuple(s) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", str(s))[:3])


def _tomcat_newer(latest: str, installed: str) -> bool:
    if not latest or not installed or installed == "unknown":
        return False
    return _patch_tuple(latest) > _patch_tuple(installed)


def check(force: bool = False, now: float = None) -> Dict:
    """Return {java, tomcat, errors, checked_at, cached}. Uses the TTL cache
    unless `force`. `now` is injectable for tests."""
    now = now if now is not None else time.time()
    if not force:
        c = _read_cache()
        if c and (now - float(c.get("checked_at", 0))) < TTL_SECONDS:
            c["cached"] = True
            return c

    java_res: Dict[str, Dict] = {}
    tomcat_res: Dict[str, Dict] = {}
    errors: Dict[str, str] = {}

    for m in java.plugin_majors():
        installed = java.installed_jdk_version(m)
        latest = None
        try:
            latest = java.resolve_latest_jdk(m)
        except Exception as e:
            errors["java-%d" % m] = str(e)
        java_res[str(m)] = {
            "installed": installed,
            "latest": latest,
            "update": java.version_newer(latest, installed),
        }

    for major in sorted(registry.LINES):
        installed = installer.is_installed(major)
        if not installed:
            continue
        latest = None
        try:
            latest = registry.resolve_latest_patch(major)
        except Exception as e:
            errors["tomcat-%s" % major] = str(e)
        tomcat_res[major] = {
            "installed": installed,
            "latest": latest,
            "update": _tomcat_newer(latest, installed),
        }

    out = {"java": java_res, "tomcat": tomcat_res, "errors": errors,
           "checked_at": now, "cached": False}
    _write_cache(out)
    return out
