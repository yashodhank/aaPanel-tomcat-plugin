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
