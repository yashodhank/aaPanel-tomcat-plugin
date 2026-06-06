# coding: utf-8
"""
Coexistence with aaPanel "System Hardening" (the `syssafe` plugin).

This registers JavaHost in syssafe's OWN process allowlist (`process_white` /
`process_white_rule`) — the sanctioned extension point — so JavaHost-managed
Tomcat/JAR services are recognized rather than killed/blocked as "abnormal
processes". It is append-only (never removes another entry), backs the config up
first, and is reversible. It does NOT disable hardening or bypass any control;
defeating an exec/anti-persistence filter is out of scope by design.

Note: this addresses the *process protection* layer. The *immutable service dir*
layer is handled separately (util.immutable lift/relock). On the most aggressive
setups a one-time hardening re-toggle may still be needed to apply changes; that
is surfaced honestly to the operator.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import List, Optional, Tuple

from ..util import immutable

# Where syssafe keeps its config (varies by panel version).
CONFIG_CANDIDATES = [
    "/www/server/panel/plugin/syssafe/config/config.json",
    "/www/server/panel/plugin/syssafe/config.json",
    "/www/server/panel/data/syssafe.json",
]

# Markers that identify JavaHost-managed processes. `java` is already in
# syssafe's default allowlist; these cover the launcher/daemon + a path rule.
JAVAHOST_RULES = ["/www/server/javahost", "javahost"]   # process_white_rule (substring)
JAVAHOST_NAMES = ["catalina.sh", "jsvc", "jsvc.exec"]    # process_white (exact)


def find_config() -> Tuple[Optional[str], Optional[dict]]:
    for p in CONFIG_CANDIDATES:
        try:
            with open(p) as f:
                d = json.load(f)
            if isinstance(d, dict) and "process" in d:
                return p, d
        except Exception:
            continue
    return None, None


def is_active() -> bool:
    _, d = find_config()
    return bool(d and d.get("open") and d.get("process", {}).get("open"))


# aaPanel's deepest layer is a global LD_PRELOAD execve filter (bt_security /
# "usranalyse"), which is what emits 203/EXEC + "Tips from BT security". It is NOT
# governed by syssafe's process_white — it has its own enable/disable + config.
LD_PRELOAD = "/etc/ld.so.preload"
EXEC_AGENT_MARKERS = ("usranalyse", "bt_security", "bt_tamper")
USRANALYSE_DISABLE = "/usr/local/usranalyse/sbin/usranalyse-disable"
USRANALYSE_ENABLE = "/usr/local/usranalyse/sbin/usranalyse-enable"


def exec_filter() -> dict:
    """Detect the global LD_PRELOAD execve filter that blocks new daemons.
    JavaHost will NOT auto-disable it (a host exec-filter is anti-persistence
    security); it reports it and the operator's sanctioned toggle."""
    active = False
    lib = ""
    try:
        with open(LD_PRELOAD) as f:
            txt = f.read()
        for line in txt.splitlines():
            if any(m in line for m in EXEC_AGENT_MARKERS):
                active = True
                lib = line.strip()
                break
    except Exception:
        pass
    return {
        "active": active,
        "library": lib,
        "toggle": USRANALYSE_DISABLE if os.path.exists(USRANALYSE_DISABLE) else "",
        "guidance": (
            "A host execve-filter (aaPanel bt_security / usranalyse, via "
            "/etc/ld.so.preload) blocks NEW services from starting (203/EXEC). "
            "JavaHost won't disable a global security preload. Authorize it in "
            "aaPanel Security -> bt_security, or temporarily run "
            "`%s` while creating apps then re-enable with `%s`."
            % (USRANALYSE_DISABLE, USRANALYSE_ENABLE)) if active else "",
    }


def merge_whitelist(cfg: dict) -> Tuple[dict, List[str]]:
    """Pure: append JavaHost markers to the process allowlists if missing.
    Returns (cfg, added). Idempotent; never removes existing entries."""
    added: List[str] = []
    proc = cfg.setdefault("process", {})
    for key, vals in (("process_white_rule", JAVAHOST_RULES),
                      ("process_white", JAVAHOST_NAMES)):
        lst = proc.setdefault(key, [])
        for v in vals:
            if v not in lst:
                lst.append(v)
                added.append("%s:%s" % (key, v))
    return cfg, added


def whitelist_javahost() -> dict:
    """Register JavaHost in syssafe's process allowlist (append-only, backed up)."""
    path, cfg = find_config()
    if not path:
        raise RuntimeError("aaPanel System Hardening (syssafe) not found — nothing to whitelist")
    cfg, added = merge_whitelist(cfg)
    if not added:
        return {"changed": False, "config": path, "added": [],
                "active": is_active(), "exec_filter": exec_filter()}
    backup = path + ".javahost.bak"
    if not os.path.exists(backup):
        shutil.copyfile(path, backup)
    body = json.dumps(cfg)
    # the config file/dir may itself be immutable on a hardened host
    with immutable.writable(os.path.dirname(path)):
        with immutable.writable(path):
            tmp = path + ".javahost.tmp"
            with open(tmp, "w") as f:
                f.write(body)
            os.replace(tmp, path)
    return {"changed": True, "config": path, "added": added, "backup": backup,
            "exec_filter": exec_filter(),
            "note": "Registered JavaHost in System Hardening's process allowlist. "
                    "If a managed service still won't start, re-toggle System "
                    "Hardening once so syssafe reloads."}
