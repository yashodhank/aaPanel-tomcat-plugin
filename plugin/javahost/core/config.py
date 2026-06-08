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

    # Log management (rotation + purge). Rotation copy-truncates oversized app/cron
    # logs (gzip, keep N) so a runaway log can't fill the disk; purge deletes
    # rotated artifacts older than the retention window. Driven by a managed
    # /etc/cron.d/javahost-logrotate (hardening-aware), configurable from Settings.
    "log_rotate_enabled": True,
    "log_rotate_when": "daily",     # daily | weekly | monthly
    "log_rotate_keep": 7,           # number of gzipped rotations kept per log
    "log_rotate_max_mb": 50,        # rotate a log once it exceeds this size
    "log_purge_days": 30,           # delete rotated *.gz older than this many days
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


def set(key: str, value):
    """Persist a single config key to config.json (atomic) and invalidate the
    mtime cache so the next get() sees it. Read-mostly file; no secrets here."""
    return update({key: value})


def update(values: dict) -> dict:
    """Merge `values` into config.json atomically. Returns the merged dict."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.update(values or {})
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    # config.json may hold a secret (e.g. aapanel_api_key) — keep it owner-only
    # (0600) rather than whatever the umask gives. Create the temp restricted,
    # not chmod-after, so it is never briefly world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        try:
            os.unlink(tmp)
        finally:
            raise
    os.chmod(tmp, 0o600)  # O_CREAT mode is masked by umask; force it
    os.replace(tmp, CONFIG_PATH)
    _CACHE.pop("k", None)  # force re-read on next get()
    return data


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def log_rotate_enabled() -> bool:
    return _as_bool(get("log_rotate_enabled", True), True)


def log_rotate_when() -> str:
    v = str(get("log_rotate_when", "daily") or "daily").strip().lower()
    return v if v in ("daily", "weekly", "monthly") else "daily"


def log_rotate_keep(default: int = 7) -> int:
    try:
        return max(0, int(get("log_rotate_keep", default) or default))
    except (TypeError, ValueError):
        return default


def log_rotate_max_mb(default: int = 50) -> int:
    try:
        return max(1, int(get("log_rotate_max_mb", default) or default))
    except (TypeError, ValueError):
        return default


def log_purge_days(default: int = 30) -> int:
    try:
        return max(0, int(get("log_purge_days", default) or default))
    except (TypeError, ValueError):
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


def site_suffix() -> str:
    """Public-domain suffix the plugin appends to an app name to form a default
    reverse-proxy domain (e.g. suffix "example.com" -> "<app>.example.com").

    Read from the plugin config key "site_suffix"; defaults to "" (empty). When
    empty there is NO baked-in domain — callers must require an explicit ?domain=
    (no FQDN is ever guessed). Never hardcoded into the shipped plugin."""
    val = get("site_suffix", "")
    return str(val).strip().strip(".") if val else ""
