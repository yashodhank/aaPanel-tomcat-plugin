# coding: utf-8
"""
The ONLY module allowed to touch aaPanel/BaoTa internals. Keeping the panel
coupling here means the rest of `core/` is a clean, portable library that can be
reused under BaoTa or other panels by swapping this adapter.

It deliberately uses the panel's public, documented helpers (public.returnMsg,
public.GetMsg, public.WriteLog) — the API surface §3.1 of the AAPANEL license
permits building against — and contains no aaPanel implementation code.
"""
from __future__ import annotations

from typing import Any

try:
    import public  # provided by the panel runtime
except Exception:  # pragma: no cover - allows import/unit-test off-panel
    public = None


def ok(data: Any = "ok"):
    if public:
        return public.returnMsg(True, data) if isinstance(data, str) else {"status": True, "msg": data}
    return {"status": True, "msg": data}


def err(msg: str):
    if public:
        return public.returnMsg(False, msg)
    return {"status": False, "msg": msg}


def log(action: str, msg: str) -> None:
    if public:
        try:
            public.WriteLog("JavaHost", "%s: %s" % (action, msg))
        except Exception:
            pass


def attr(get: Any, name: str, default=None):
    """Safe attribute access on the panel `get` namespace."""
    return getattr(get, name, default)
