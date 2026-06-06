# coding: utf-8
"""Tiny JSON config reader for JavaHost (optional /www/server/javahost/config.json)."""
from __future__ import annotations

import json
import os

CONFIG_PATH = "/www/server/javahost/config.json"

_DEFAULTS = {
    # When true (default), the plugin may momentarily lift the immutable bit on its
    # OWN service-file paths to write a unit on a hardened host, then re-lock them.
    # Set false to forbid touching chattr +i (plugin then errors and asks the
    # operator to disable hardening / lift the lock manually).
    "manage_hardening": True,
}


def get(key: str, default=None):
    if default is None:
        default = _DEFAULTS.get(key)
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


def aapanel_api_key():
    """aaPanel interface API key (api_sk) for the native HTTP API, if the operator
    chose to mirror it into the plugin config. Returns None when unset — the SSL
    orchestrator then SKIPS the native path and goes straight to certbot. Never
    hardcoded; never a secret baked into the plugin."""
    val = get("aapanel_api_key", None)
    return str(val) if val else None


def aapanel_port(default: int = 37778):
    """Local aaPanel panel port for loopback API calls (default 37778). Read from
    plugin config if present."""
    try:
        return int(get("aapanel_port", default) or default)
    except (TypeError, ValueError):
        return default
