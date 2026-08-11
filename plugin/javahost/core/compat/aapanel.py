# coding: utf-8
"""
The ONLY module allowed to touch aaPanel/BaoTa internals. Keeping the panel
coupling here means the rest of `core/` is a clean, portable library that can be
reused under BaoTa or other panels by swapping this adapter.

It deliberately uses the panel's public, documented helpers (public.returnMsg,
public.GetMsg, public.WriteLog) — the API surface §3.1 of the AAPANEL license
permits building against — and contains no aaPanel implementation code.

Also owns loopback HTTP API helpers (token auth, AddSite/CreateProxy/ACME) so
deploy/ssl do not re-implement panel coupling.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl as _sslmod
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .. import config

try:
    import public  # provided by the panel runtime
except Exception:  # pragma: no cover - allows import/unit-test off-panel
    public = None

# Common aaPanel panel-port file (single integer).
_PORT_PL = "/www/server/panel/data/port.pl"


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


# --- loopback HTTP API -------------------------------------------------------

def detect_panel_port(default: int = 37778) -> int:
    """Best-effort panel port: config override, else port.pl, else default."""
    try:
        cfg = config.get("aapanel_port", None)
        if cfg is not None and str(cfg).strip() != "":
            return int(cfg)
    except (TypeError, ValueError):
        pass
    try:
        with open(_PORT_PL, encoding="utf-8", errors="replace") as f:
            raw = f.read().strip()
        if raw.isdigit():
            return int(raw)
    except OSError:
        pass
    return int(default)


def panel_port() -> int:
    """Effective panel port for loopback calls (config → port.pl → 37778)."""
    return detect_panel_port(37778)


def _api_token(api_sk: str, request_time: int) -> str:
    # MD5 is mandated by aaPanel's API token scheme — not our security primitive.
    sk_md5 = hashlib.md5(api_sk.encode()).hexdigest()  # nosec B324
    return hashlib.md5((str(request_time) + sk_md5).encode()).hexdigest()  # nosec B324


def _ssl_ctx():
    ctx = _sslmod.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _sslmod.CERT_NONE
    return ctx


def http_request(path: str, *, params: Optional[Dict] = None,
                 body: Optional[Dict] = None, method: str = "GET",
                 timeout: float = 30) -> Tuple[Optional[Dict], Optional[str]]:
    """Authenticated loopback call to the panel HTTP API.

    ``path`` is like ``/site`` or ``/acme``. Auth query fields are added from
    ``aapanel_api_key``. Returns ``(parsed_json_or_None, error_or_None)``.
    """
    api_sk = config.aapanel_api_key()
    if not api_sk:
        return None, "aapanel_api_key not configured"
    try:
        request_time = int(time.time())
        token = _api_token(api_sk, request_time)
        q = dict(params or {})
        q["request_time"] = str(request_time)
        q["request_token"] = token
        port = detect_panel_port()
        url = "https://127.0.0.1:%d%s?%s" % (
            port, path, urllib.parse.urlencode(q))
        data = (urllib.parse.urlencode(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method.upper())
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None, "non-JSON panel response"
        if isinstance(parsed, dict):
            return parsed, None
        return None, "unexpected panel response type"
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:
        return None, str(e)


def http_add_site_with_proxy(domain: str, port: int) -> Optional[Dict]:
    """Create an aaPanel site and attach a reverse-proxy to 127.0.0.1:<port>.

    AddSite alone with type=proxy does NOT configure the upstream on typical
    aaPanel builds — CreateProxy must follow. Returns a success dict or None.
    """
    backend = "http://127.0.0.1:%d" % int(port)
    add_body = {
        "webname": json.dumps({"domain": domain, "domainlist": [], "count": 0}),
        "path": "/www/wwwroot/%s" % domain,
        "type": "PHP",
        "type_id": "0",
        "version": "00",
        "port": "80",
        "ps": "JavaHost: %s -> %s" % (domain, backend),
        "ftp": "false",
        "sql": "false",
        "set_ssl": "0",
        # Extra fields some builds honor on AddSite (harmless if ignored).
        "proxy_pass": backend,
        "proxysite": backend,
    }
    data, err_msg = http_request(
        "/site", params={"action": "AddSite"}, body=add_body,
        method="POST", timeout=30)
    # Treat "site already exists" as OK so we can still attach/repair the proxy.
    site_ok = False
    if isinstance(data, dict):
        if data.get("status") or data.get("siteStatus"):
            site_ok = True
        else:
            msg = str(data.get("msg") or data.get("error") or "").lower()
            if "exist" in msg or "already" in msg:
                site_ok = True
    if not site_ok:
        return None

    proxy_body = {
        "proxyname": "javahost-" + domain.replace(".", "-")[:40],
        "sitename": domain,
        "proxydir": "/",
        "proxysite": backend,
        "todomain": "$host",
        "type": "1",
        "cache": "0",
        "subfilter": "[]",
        "advanced": "0",
        "cachetime": "1",
        "nocheck": "1",
    }
    pdata, perr = http_request(
        "/site", params={"action": "CreateProxy"}, body=proxy_body,
        method="POST", timeout=30)
    if isinstance(pdata, dict) and (pdata.get("status") or pdata.get("siteStatus")):
        return {"ok": True, "path": "aapanel-http",
                "detail": "via HTTP AddSite+CreateProxy -> %s" % backend}
    # Some panels expose ModifyProxy / SetProxy instead.
    for action in ("ModifyProxy", "SetProxy", "create_proxy"):
        pdata2, _ = http_request(
            "/site", params={"action": action}, body=proxy_body,
            method="POST", timeout=30)
        if isinstance(pdata2, dict) and (pdata2.get("status") or pdata2.get("siteStatus")):
            return {"ok": True, "path": "aapanel-http",
                    "detail": "via HTTP AddSite+%s -> %s" % (action, backend)}
    # Site exists but proxy attach failed — not a success (would be a dead site).
    return None


def http_remove_site(domain: str) -> bool:
    """Delete a site via getData lookup + DeleteSite. Returns True on success."""
    result, err_msg = http_request(
        "/data",
        params={"action": "getData", "table": "sites", "search": domain},
        method="GET", timeout=15)
    if not isinstance(result, dict):
        return False
    site_id = None
    data_list = result.get("data") or result.get("msg") or []
    if isinstance(data_list, list):
        for row in data_list:
            if isinstance(row, dict) and row.get("name") == domain:
                site_id = row.get("id")
                break
    if not site_id:
        return False
    data2, _ = http_request(
        "/site", params={"action": "DeleteSite"},
        body={"id": str(site_id), "webname": domain},
        method="POST", timeout=30)
    return bool(isinstance(data2, dict) and data2.get("status"))


def http_apply_cert(domain: str) -> Optional[bool]:
    """Issue via aaPanel native ACME. True/False on result; None if no api key."""
    if not config.aapanel_api_key():
        return None
    data, err_msg = http_request(
        "/acme", params={"action": "apply_cert_api"},
        body={
            "domains": json.dumps([domain]),
            "siteName": domain,
            "auth_type": "http",
            "auth_to": domain,
        },
        method="POST", timeout=120)
    if not isinstance(data, dict):
        return False
    return bool(data.get("status") or data.get("success") or data.get("cert"))


def http_enable_site_ssl(domain: str) -> bool:
    """Ask aaPanel to enable HTTPS on an existing site (panel-owned vhost).

    Tries SetSSL / HttpToHttps style actions after apply_cert_api. Best-effort:
    returns True if any call reports success.
    """
    if not config.aapanel_api_key():
        return False
    ok_any = False
    # apply_cert_api often both issues and installs into the site's panel cert dir
    applied = http_apply_cert(domain)
    if applied:
        ok_any = True
    for action, body in (
        ("SetSSL", {"type": "1", "siteName": domain, "updateOf": "1"}),
        ("HttpToHttps", {"siteName": domain}),
        ("set_https_mode", {"siteName": domain}),
    ):
        data, _ = http_request(
            "/site", params={"action": action}, body=body,
            method="POST", timeout=60)
        if isinstance(data, dict) and (data.get("status") or data.get("success")):
            ok_any = True
    return ok_any


def http_disable_site_ssl(domain: str) -> bool:
    """Revert aaPanel site to HTTP-only (cert kept)."""
    if not config.aapanel_api_key():
        return False
    data, _ = http_request(
        "/site", params={"action": "CloseToHttps"},
        body={"siteName": domain}, method="POST", timeout=30)
    if isinstance(data, dict) and data.get("status"):
        return True
    data2, _ = http_request(
        "/site", params={"action": "HttpToHttps"},
        body={"siteName": domain, "close": "1"}, method="POST", timeout=30)
    return bool(isinstance(data2, dict) and data2.get("status"))


def api_key_configured() -> bool:
    return bool(config.aapanel_api_key())
