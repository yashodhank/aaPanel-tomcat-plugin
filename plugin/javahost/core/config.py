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


# mtime-based cache: avoids re-opening/parsing config.json on every get() (it is
# read on many hot paths) WITHOUT going stale — the file is re-read only when its
# mtime/size changes, so a config edit takes effect immediately.
_CACHE = {}


def _load() -> dict:
    try:
        st = os.stat(CONFIG_PATH)
    except OSError:
        _CACHE.pop("k", None)
        return {}
    key = (st.st_mtime_ns, st.st_size)
    ent = _CACHE.get("k")
    if ent and ent[0] == key:
        return ent[1]
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _CACHE["k"] = (key, data)
    return data


def get(key: str, default=None):
    if default is None:
        default = _DEFAULTS.get(key)
    return _load().get(key, default)


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


def site_suffix() -> str:
    """Public-domain suffix the plugin appends to an app name to form a default
    reverse-proxy domain (e.g. suffix "example.com" -> "<app>.example.com").

    Read from the plugin config key "site_suffix"; defaults to "" (empty). When
    empty there is NO baked-in domain — callers must require an explicit ?domain=
    (no FQDN is ever guessed). Never hardcoded into the shipped plugin."""
    val = get("site_suffix", "")
    return str(val).strip().strip(".") if val else ""
