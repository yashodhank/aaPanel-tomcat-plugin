# coding: utf-8
"""
Plugin Danger-zone maintenance: granular, typed-confirm, dry-run-first wipe.

The Settings UI uses this to remove plugin-managed state in a controlled way.
Everything here is DEFENSIVE by construction:

  * It NEVER touches /usr/local/btjdk (the panel-owned JDK), the panel cert,
    other plugins' configs, or any database.
  * JDK removal is restricted to /www/server/javahost/runtimes/jdk-* only.
  * `wipe()` is a no-op unless the caller passes the exact typed confirmation
    string "WIPE", and each category is run in its own try/except so one
    failure can't abort the rest. Each step reports {removed, errors}.

Categories: apps, jdks, tomcats, sites, full (= apps+jdks+tomcats+sites then the
whole data root). Default (empty scope) removes nothing.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List

from .util import fs
from .runtime import java
from .tomcat import instance, installer, service, registry
from .deploy import proxy

DATA_ROOT = "/www/server/javahost"
CONFIRM = "WIPE"

_VALID_SCOPE = ("apps", "jdks", "tomcats", "sites", "full")
# Only plugin-managed runtimes — the panel's /usr/local/btjdk is NEVER in scope.
_JDK_RE = re.compile(r"^jdk-\d+$")


# --------------------------------------------------------------------------- #
# discovery helpers (no removals)
# --------------------------------------------------------------------------- #
def _list_apps() -> List[str]:
    root = instance.INSTANCE_ROOT
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root)
                  if os.path.isdir(os.path.join(root, n)))


def _list_plugin_jdks() -> List[str]:
    """Plugin-owned runtime dir names under JDK_ROOT (jdk-<major> only).

    Deliberately scans ONLY java.JDK_ROOT, so the panel's /usr/local/btjdk can
    never be listed (or removed)."""
    root = java.JDK_ROOT
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root)
                  if _JDK_RE.match(n) and os.path.isdir(os.path.join(root, n)))


def _list_installed_tomcats() -> List[str]:
    out: List[str] = []
    for major in sorted(registry.LINES):
        if installer.is_installed(major):
            out.append(major)
    return out


def _list_sites() -> List[str]:
    """Vhost files the plugin owns under proxy.VHOST_DIR (basenames)."""
    vdir = proxy.VHOST_DIR
    if not os.path.isdir(vdir):
        return []
    return sorted(f for f in os.listdir(vdir) if f.endswith(".conf"))


def _dir_size(path: str) -> int:
    """Best-effort recursive size in bytes (never raises)."""
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------- #
# preview (dry-run)
# --------------------------------------------------------------------------- #
def wipe_preview() -> Dict:
    """Counts + lists of what each category would remove WITHOUT removing
    anything. jdks lists plugin-owned runtimes only (never the panel btjdk)."""
    apps = _list_apps()
    jdks = _list_plugin_jdks()
    tomcats = _list_installed_tomcats()
    sites = _list_sites()
    size = _dir_size(DATA_ROOT)
    return {
        "apps": {"count": len(apps), "items": apps},
        "jdks": {"count": len(jdks), "items": jdks},
        "tomcats": {"count": len(tomcats), "items": tomcats},
        "sites": {"count": len(sites), "items": sites},
        "data_root": {
            "path": DATA_ROOT,
            "exists": os.path.isdir(DATA_ROOT),
            "size_bytes": size,
            "size_mb": round(size / (1 << 20), 1),
        },
        "confirm_required": CONFIRM,
    }


# --------------------------------------------------------------------------- #
# wipe (destructive, typed-confirm gated)
# --------------------------------------------------------------------------- #
def _normalize_scope(scope) -> List[str]:
    if isinstance(scope, str):
        items = [s.strip() for s in scope.split(",")]
    else:
        items = [str(s).strip() for s in (scope or [])]
    out: List[str] = []
    for s in items:
        if not s:
            continue
        if s not in _VALID_SCOPE:
            raise ValueError("invalid wipe scope item: %r" % s)
        if s not in out:
            out.append(s)
    return out


def _wipe_apps() -> Dict:
    removed: List[str] = []
    errors: Dict[str, str] = {}
    for app in _list_apps():
        try:
            # stop + disable the service first (immutable-aware via service layer),
            # then delete the instance (marker-gated by instance.delete).
            try:
                service.action(app, "stop")
            except Exception:
                pass
            service.remove_unit(app)  # disables + removes the unit (hardening-aware)
            instance.delete(app)
            removed.append(app)
        except Exception as e:
            errors[app] = str(e)
    return {"removed": removed, "errors": errors}


def _wipe_jdks() -> Dict:
    """Remove plugin-owned JDKs NOT in use by any app. A JDK still pinned by a
    deployed app is SKIPPED (removing it would break the app on next restart).
    In a full/apps wipe the apps are removed first, so nothing is pinned and
    every JDK is removed."""
    removed: List[str] = []
    errors: Dict[str, str] = {}
    skipped: Dict[str, List[str]] = {}
    root = java.JDK_ROOT
    for name in _list_plugin_jdks():
        m = re.match(r"jdk-(\d+)", name)
        users = java.usage(int(m.group(1))) if m else []
        if users:
            skipped[name] = users
            continue
        target = os.path.join(root, name)
        try:
            fs.safe_rmtree(target, require_marker=fs.is_managed(target))
            removed.append(name)
        except Exception as e:
            errors[name] = str(e)
    return {"removed": removed, "errors": errors, "skipped": skipped}


def _wipe_tomcats() -> Dict:
    """Uninstall Tomcat lines NOT in use by any app (an app on that Tomcat would
    break). Skipped when in use; apps are wiped first in a full/apps wipe."""
    removed: List[str] = []
    errors: Dict[str, str] = {}
    skipped: Dict[str, List[str]] = {}
    apps = instance.list_apps()
    for major in _list_installed_tomcats():
        users = [a["app"] for a in apps if str(a.get("tomcat")) == str(major)]
        if users:
            skipped[major] = users
            continue
        try:
            installer.uninstall(major)
            removed.append(major)
        except Exception as e:
            errors[major] = str(e)
    return {"removed": removed, "errors": errors, "skipped": skipped}


def _wipe_sites() -> Dict:
    removed: List[str] = []
    errors: Dict[str, str] = {}
    for conf in _list_sites():
        app = conf[:-len(".conf")]
        try:
            proxy.remove_site(app)
            removed.append(app)
        except Exception as e:
            errors[app] = str(e)
    # inverse of ensure_include: drop the include line we added, then reload.
    include_removed = False
    try:
        include_removed = _remove_include()
    except Exception as e:
        errors["__include__"] = str(e)
    try:
        proxy.reload_nginx()
    except Exception:
        pass
    return {"removed": removed, "errors": errors, "include_removed": include_removed}


def _remove_include(nginx_conf: str = None) -> bool:
    """Remove the JavaHost vhost include line from nginx.conf (inverse of
    proxy.ensure_include). Validates the rewritten config and restores on
    failure. Returns True if a line was removed."""
    nginx_conf = nginx_conf or proxy.NGINX_CONF
    if not os.path.isfile(nginx_conf):
        return False
    with open(nginx_conf, encoding="utf-8", errors="replace") as f:
        content = f.read()
    if proxy.VHOST_DIR not in content:
        return False
    lines = content.splitlines(keepends=True)
    kept = [ln for ln in lines
            if not (proxy.VHOST_DIR in ln and "include" in ln)]
    if len(kept) == len(lines):
        return False
    new_content = "".join(kept)
    fs.atomic_write(nginx_conf, new_content, mode=0o644)
    if not proxy.nginx_test():
        fs.atomic_write(nginx_conf, content, mode=0o644)
        return False
    return True


def _wipe_full() -> Dict:
    """apps + jdks + tomcats + sites, THEN remove the whole data root (guarded)."""
    steps = {
        "apps": _wipe_apps(),
        "sites": _wipe_sites(),
        "tomcats": _wipe_tomcats(),
        "jdks": _wipe_jdks(),
    }
    data_root = {"removed": False, "errors": {}}
    try:
        if os.path.isdir(DATA_ROOT):
            # guarded: safe_rmtree refuses anything outside MANAGED_ROOTS and
            # DATA_ROOT is the managed root itself.
            fs.safe_rmtree(DATA_ROOT, require_marker=fs.is_managed(DATA_ROOT))
            data_root["removed"] = True
    except Exception as e:
        data_root["errors"]["data_root"] = str(e)
    steps["data_root"] = data_root
    return steps


def wipe(scope, confirm: str) -> Dict:
    """Remove plugin-managed state for the given `scope`.

    Only proceeds when confirm == "WIPE"; otherwise returns a no-op summary.
    `scope` is a csv string or list drawn from {apps,jdks,tomcats,sites,full}.
    Never touches /usr/local/btjdk, the panel cert, other plugins' configs, or
    any database. Returns a per-category {removed, errors} summary.
    """
    items = _normalize_scope(scope)
    if confirm != CONFIRM:
        return {"ok": False, "performed": False,
                "reason": "confirmation required: pass confirm=%r" % CONFIRM,
                "scope": items}
    if not items:
        return {"ok": True, "performed": False, "reason": "empty scope",
                "scope": items}

    results: Dict[str, Dict] = {}
    if "full" in items:
        results = _wipe_full()
        return {"ok": True, "performed": True, "scope": ["full"], "steps": results}

    if "apps" in items:
        results["apps"] = _wipe_apps()
    if "sites" in items:
        results["sites"] = _wipe_sites()
    if "tomcats" in items:
        results["tomcats"] = _wipe_tomcats()
    if "jdks" in items:
        results["jdks"] = _wipe_jdks()
    return {"ok": True, "performed": True, "scope": items, "steps": results}
