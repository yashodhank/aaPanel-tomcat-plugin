# coding: utf-8
"""
Read-only detection of aaPanel "System Hardening" (the `syssafe` plugin) and the
host exec filter. JavaHost reports what it finds so the operator can act in the
panel; it never edits the hardening plugin's config or allowlists. Pausing
hardening for a system write goes through the panel's own helper, in
`compat.aapanel.system_write`.
"""
from __future__ import annotations

import json
import os
from typing import Optional, Tuple

# Where syssafe keeps its config (varies by panel version).
CONFIG_CANDIDATES = [
    "/www/server/panel/plugin/syssafe/config/config.json",
    "/www/server/panel/plugin/syssafe/config.json",
    "/www/server/panel/data/syssafe.json",
]


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
# governed by syssafe's process allowlist — it has its own enable/disable + config.
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
