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
import re
import ssl as _sslmod
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .. import config
from ..util import fs, validate
from ..util.shell import run

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


# --- webserver gate (nginx-only until Apache/OLS epic) -----------------------

_SUPPORTED_WEBSERVER = "nginx"


def detect_webserver() -> str:
    """Best-effort webserver id: nginx | apache | openlitespeed | unknown.

    Prefers ``public.get_webserver()`` when running inside the panel. Falls back
    to aaPanel filesystem layout only when exactly one known server tree is
    present. Missing or conflicting evidence reports ``unknown``; callers that
    write nginx configuration therefore fail closed.
    """
    if public is not None:
        try:
            ws = str(public.get_webserver() or "").strip().lower()
            if ws in ("nginx", "apache"):
                return ws
            if ws in ("openlitespeed", "ols", "open_litespeed", "open-litespeed"):
                return "openlitespeed"
            if ws:
                return ws
        except Exception:
            pass
    has_nginx = os.path.isfile("/www/server/nginx/sbin/nginx")
    has_apache = (
        os.path.isfile("/www/server/apache/bin/httpd")
        or os.path.isfile("/www/server/apache/bin/apachectl")
    )
    has_ols = (
        os.path.isdir("/usr/local/lsws/conf")
        or os.path.isdir("/www/server/panel/vhost/openlitespeed")
    )
    detected = [
        name for name, present in (
            ("nginx", has_nginx),
            ("apache", has_apache),
            ("openlitespeed", has_ols),
        ) if present
    ]
    return detected[0] if len(detected) == 1 else "unknown"


def require_nginx() -> Optional[str]:
    """Return a clear error when the active webserver is not nginx, else None."""
    ws = detect_webserver()
    if ws == _SUPPORTED_WEBSERVER:
        return None
    return (
        "JavaHost reverse proxy and SSL require nginx (detected %s). "
        "Apache and OpenLiteSpeed are not supported yet — switch the panel "
        "webserver to nginx, or wait for a future release." % ws
    )


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


def _lookup_site_row(domain: str) -> Optional[Dict]:
    """Return the aaPanel sites-table row for ``domain``, or None."""
    result, _ = http_request(
        "/data",
        params={"action": "getData", "table": "sites", "search": domain},
        method="GET", timeout=15)
    if not isinstance(result, dict):
        return None
    data_list = result.get("data") or result.get("msg") or []
    if not isinstance(data_list, list):
        return None
    for row in data_list:
        if isinstance(row, dict) and row.get("name") == domain:
            return row
    return None


def _created_site_id(result: Dict) -> Optional[str]:
    """Extract an explicit, stable site id from a successful AddSite result."""
    candidates = [result]
    for key in ("data", "msg"):
        nested = result.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for item in candidates:
        for key in ("id", "site_id", "siteId"):
            value = item.get(key)
            if isinstance(value, bool):
                continue
            raw = str(value or "").strip()
            if raw.isdigit() and int(raw) > 0:
                return raw
    return None


_SITE_PS_RE = re.compile(
    r"\AJavaHost: (?P<domain>[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?) "
    r"-> http://127\.0\.0\.1:(?P<port>[0-9]{1,5})\Z"
)


def _site_ps_is_javahost(ps: Any, domain: str) -> bool:
    """Require the exact JavaHost marker grammar for the requested domain."""
    try:
        expected_domain = validate.domain(domain)
    except (TypeError, ValueError):
        return False
    match = _SITE_PS_RE.fullmatch(str(ps or ""))
    if not match or match.group("domain") != expected_domain:
        return False
    try:
        validate.port(match.group("port"))
    except (TypeError, ValueError):
        return False
    return True


def http_add_site_with_proxy(domain: str, port: int) -> Optional[Dict]:
    """Create an aaPanel site and attach a reverse-proxy to 127.0.0.1:<port>.

    AddSite alone does NOT configure the upstream on typical aaPanel builds —
    CreateProxy must follow. Returns a success dict, or a dict with
    ``ok=False``/``error`` when the site was created but proxy attach failed
    (so callers do not silently fall through and leave orphans undiagnosed),
    or None when the API key is missing / AddSite itself failed.

    Fail-closed on non-nginx. Reuses an existing site only when its ``ps``
    has an exact JavaHost marker bound to this domain; otherwise returns a
    conflict error. A failed attach never deletes the aaPanel site automatically
    because DeleteSite has no conditional ownership precondition.
    """
    nginx_err = require_nginx()
    if nginx_err:
        return {"ok": False, "path": "aapanel-http", "error": nginx_err}

    backend = "http://127.0.0.1:%d" % int(port)
    # CreateProxy writes under vhost/nginx/proxy/<site>/ — if the parent dir is
    # missing (fresh panels), the call fails silently / with a vague error.
    try:
        os.makedirs("/www/server/panel/vhost/nginx/proxy", mode=0o755, exist_ok=True)
        os.makedirs(
            "/www/server/panel/vhost/nginx/proxy/%s" % domain,
            mode=0o755, exist_ok=True)
    except OSError:
        pass

    ownership_marker = "JavaHost: %s -> %s" % (domain, backend)
    add_body = {
        "webname": json.dumps({"domain": domain, "domainlist": [], "count": 0}),
        "path": "/www/wwwroot/%s" % domain,
        "type": "PHP",
        "type_id": "0",
        "version": "00",
        "port": "80",
        "ps": ownership_marker,
        "ftp": "false",
        "sql": "false",
        "set_ssl": "0",
        "proxy_pass": backend,
        "proxysite": backend,
    }
    data, err_msg = http_request(
        "/site", params={"action": "AddSite"}, body=add_body,
        method="POST", timeout=30)
    site_ok = False
    created_by_us = False
    created_site_id = None
    reused_existing = False
    if isinstance(data, dict):
        if data.get("status") or data.get("siteStatus"):
            site_ok = True
            created_by_us = True
            created_site_id = _created_site_id(data)
        else:
            msg = str(data.get("msg") or data.get("error") or "").lower()
            if "exist" in msg or "already" in msg:
                row = _lookup_site_row(domain)
                ps = (row or {}).get("ps") if isinstance(row, dict) else ""
                if _site_ps_is_javahost(ps, domain):
                    site_ok = True
                    reused_existing = True
                else:
                    detail = (str(ps).strip() if ps else "no JavaHost marker in site notes")
                    return {
                        "ok": False,
                        "path": "aapanel-http",
                        "conflict": True,
                        "error": (
                            "Domain %s already exists in aaPanel and is not a "
                            "JavaHost site (%s). Remove or rename it under "
                            "Website, or choose another domain."
                            % (domain, detail)
                        ),
                        "detail": msg or "site already exists",
                    }
    if not site_ok:
        return None

    # Prior failed CreateProxy attempts may have left an inline `location`
    # block in the site conf; scrub before retrying so nginx -t can pass.
    # Keep the pre-scrub body so we can restore if attach ultimately fails.
    _, scrub_snap = scrub_site_inline_proxy(domain)

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
    # AddSite → CreateProxy race: panel may not have flushed the site conf yet.
    last_detail = ""
    for attempt in range(1, 4):
        if attempt > 1:
            time.sleep(0.8 * attempt)
            scrub_site_inline_proxy(domain)
        pdata, perr = http_request(
            "/site", params={"action": "CreateProxy"}, body=proxy_body,
            method="POST", timeout=30)
        if isinstance(pdata, dict) and (pdata.get("status") or pdata.get("siteStatus")):
            out = {"ok": True, "path": "aapanel-http",
                   "detail": "via HTTP AddSite+CreateProxy -> %s" % backend}
            if reused_existing:
                out["reused"] = True
            return out
        last_detail = ""
        if isinstance(pdata, dict):
            last_detail = str(pdata.get("msg") or pdata.get("error") or pdata)
        elif perr:
            last_detail = perr
        for action in ("ModifyProxy", "SetProxy"):
            pdata2, _ = http_request(
                "/site", params={"action": action}, body=proxy_body,
                method="POST", timeout=30)
            if isinstance(pdata2, dict) and (pdata2.get("status") or pdata2.get("siteStatus")):
                out = {"ok": True, "path": "aapanel-http",
                       "detail": "via HTTP AddSite+%s -> %s" % (action, backend)}
                if reused_existing:
                    out["reused"] = True
                return out

    # CreateProxy is flaky on some aaPanel builds (injects `location` into the
    # wrong conf context). Fall back to writing the panel's own proxy include
    # layout so the site stays aaPanel-visible and nginx -t clean.
    fb_ok, fb_err = write_aapanel_proxy_files(domain, int(port))
    if fb_ok:
        out = {"ok": True, "path": "aapanel-http-fallback",
               "detail": "via AddSite + panel proxy files -> %s (CreateProxy was: %s)"
               % (backend, last_detail or "failed")}
        if reused_existing:
            out["reused"] = True
        return out

    # Total failure — restore pre-scrub site conf so we don't leave a blanked
    # reverse-proxy when CreateProxy/fallback both failed.
    _restore_site_conf(domain, scrub_snap)
    site_context = ("new aaPanel site" if created_by_us
                    else "existing JavaHost site")
    base_err = ("Proxy attach failed for %s %s -> %s: %s"
                % (site_context, domain, backend,
                   last_detail or fb_err or "no detail"))
    orphan_hint = (
        "; the site may remain in aaPanel. Verify Website entry %s%s and "
        "remove it manually only if it is still the failed JavaHost site, or "
        "fix nginx and retry SetSite"
        % (domain, " (site id %s)" % created_site_id if created_site_id else "")
        if created_by_us else
        "; the existing JavaHost site was not deleted — fix nginx and retry"
    )
    result = {
        "ok": False,
        "path": "aapanel-http",
        "error": base_err + orphan_hint,
        "detail": last_detail or fb_err or "CreateProxy failed",
        "site_may_remain": bool(created_by_us),
        "rollback_refused": bool(created_by_us),
        "reused": bool(reused_existing),
    }
    if created_site_id:
        result["site_id"] = created_site_id
    if created_by_us:
        result["manual_cleanup"] = (
            "In aaPanel Website, verify domain %s and its JavaHost marker before "
            "manually deleting the failed site." % domain
        )
    return result


_AAPANEL_SITE_CONF = "/www/server/panel/vhost/nginx/%s.conf"
_AAPANEL_PROXY_DIR = "/www/server/panel/vhost/nginx/proxy/%s"
# Panel Website → Reverse Proxy UI reads this JSON list (not nginx alone).
_AAPANEL_PROXYFILE = "/www/server/panel/data/proxyfile.json"
# CreateProxy sometimes injects this block into the wrong nginx context.
_PROXY_INLINE_RE = re.compile(
    r"[ \t]*#PROXY-START/.*?#PROXY-END/\s*",
    re.DOTALL | re.IGNORECASE,
)


def scrub_site_inline_proxy(domain: str) -> Tuple[bool, str]:
    """Remove inline #PROXY-START..#PROXY-END blocks from the site vhost conf.

    Returns (rewrote, previous_body_or_empty). previous_body is set when the
    file was rewritten so callers can restore on later failure.
    """
    site_conf = _AAPANEL_SITE_CONF % domain
    if not os.path.isfile(site_conf):
        return False, ""
    try:
        with open(site_conf, encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError:
        return False, ""
    new_body = _PROXY_INLINE_RE.sub("", body)
    if new_body == body:
        return False, ""
    try:
        fs.atomic_write(site_conf, new_body, mode=0o644)
    except OSError:
        return False, ""
    return True, body


def _restore_site_conf(domain: str, body: str) -> None:
    if not body:
        return
    site_conf = _AAPANEL_SITE_CONF % domain
    try:
        fs.atomic_write(site_conf, body, mode=0o644)
    except OSError:
        pass


def _register_proxyfile(domain: str, port: int) -> Tuple[bool, str]:
    """Upsert a proxy row so Website → Reverse Proxy lists the upstream."""
    backend = "http://127.0.0.1:%d" % int(port)
    proxyname = "javahost-" + domain.replace(".", "-")[:40]
    entry = {
        "proxyname": proxyname,
        "sitename": domain,
        "proxydir": "/",
        "proxysite": backend,
        "todomain": "$host",
        "type": 1,
        "cache": 0,
        "subfilter": [
            {"sub1": "", "sub2": ""},
            {"sub1": "", "sub2": ""},
            {"sub1": "", "sub2": ""},
        ],
        "advanced": 0,
        "cachetime": 1,
        "keepurl": 1,
        "rewritedn": [],
    }
    path = _AAPANEL_PROXYFILE
    rows: list = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read().strip() or "[]"
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return False, "unexpected proxyfile.json shape (want list)"
            rows = parsed
        except (OSError, ValueError, TypeError) as e:
            return False, "cannot read proxyfile.json: %s" % e
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Replace same site+dir or same JavaHost proxyname.
        if row.get("sitename") == domain and str(row.get("proxydir") or "/") == "/":
            continue
        if row.get("proxyname") == proxyname:
            continue
        kept.append(row)
    kept.append(entry)
    try:
        parent = os.path.dirname(path)
        if parent:
            fs.ensure_dir(parent, mode=0o755)
        fs.atomic_write(path, json.dumps(kept, ensure_ascii=False), mode=0o644)
    except OSError as e:
        return False, "cannot write proxyfile.json: %s" % e
    return True, ""


def _proxy_conf_body(backend: str) -> str:
    """aaPanel-shaped proxy snippet (panel UI parses #PROXY-START/#PROXY-END)."""
    return (
        "#PROXY-START/\n"
        "location /\n"
        "{\n"
        "    proxy_pass %s;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header X-Real-IP $remote_addr;\n"
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "    proxy_set_header REMOTE-HOST $remote_addr;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        "    proxy_set_header Connection $connection_upgrade;\n"
        "    proxy_http_version 1.1;\n"
        "    # proxy_hide_header Upgrade;\n"
        "\n"
        "    add_header X-Cache $upstream_cache_status;\n"
        "\n"
        "    #Set Nginx Cache\n"
        "    \n"
        "    set $static_file_jh 0;\n"
        "    if ( $uri ~* \"\\.(gif|png|jpg|css|js|woff|woff2)$\" )\n"
        "    {\n"
        "    	set $static_file_jh 1;\n"
        "    	expires 1m;\n"
        "    }\n"
        "    if ( $static_file_jh = 0 )\n"
        "    {\n"
        "    add_header Cache-Control no-cache;\n"
        "    }\n"
        "}\n"
        "#PROXY-END/\n"
    ) % backend


def _ensure_site_proxy_include(site_body: str, include_line: str, domain: str) -> str:
    """Ensure `include .../proxy/<domain>/*.conf;` sits in the site server block."""
    # Strip inline proxy blocks CreateProxy may have injected into the wrong context.
    site_body = _PROXY_INLINE_RE.sub("", site_body)
    if include_line in site_body or ("/proxy/%s/" % domain) in site_body:
        return site_body
    # Prefer aaPanel's usual spot (after rewrite include).
    marker = "#REWRITE-END"
    idx = site_body.find(marker)
    if idx >= 0:
        insert_at = idx + len(marker)
        return (site_body[:insert_at]
                + "\n\n    %s" % include_line
                + site_body[insert_at:])
    m = re.search(r"\bserver\s*\{", site_body)
    if not m:
        raise ValueError("no server{} block in site conf")
    brace = site_body.index("{", m.start())
    return (site_body[: brace + 1]
            + "\n    %s\n" % include_line
            + site_body[brace + 1:])


def write_aapanel_proxy_files(domain: str, port: int) -> Tuple[bool, str]:
    """Write aaPanel-layout reverse-proxy conf + ensure site include line.

    Layout matches what CreateProxy produces so the panel UI lists the proxy:
      /www/server/panel/vhost/nginx/proxy/<domain>/<md5>_<domain>.conf
      include .../proxy/<domain>/*.conf;  inside the site server{} block
      /www/server/panel/data/proxyfile.json entry (Website UI source of truth)
    Returns (ok, error_or_empty). Rolls back site conf / proxyfile / proxy
    snippet when ``nginx -t`` fails after the write. Fail-closed when the
    active webserver is not nginx.
    """
    nginx_err = require_nginx()
    if nginx_err:
        return False, nginx_err
    backend = "http://127.0.0.1:%d" % int(port)
    site_conf = _AAPANEL_SITE_CONF % domain
    proxy_dir = _AAPANEL_PROXY_DIR % domain
    include_line = "include %s/*.conf;" % proxy_dir
    if not os.path.isfile(site_conf):
        return False, "aaPanel site conf missing: %s" % site_conf
    try:
        fs.ensure_dir(proxy_dir, mode=0o755)
    except OSError as e:
        return False, "cannot create proxy dir: %s" % e

    try:
        with open(site_conf, encoding="utf-8", errors="replace") as f:
            site_snap = f.read()
    except OSError as e:
        return False, "cannot read site conf: %s" % e

    proxyfile_snap = None
    if os.path.isfile(_AAPANEL_PROXYFILE):
        try:
            with open(_AAPANEL_PROXYFILE, encoding="utf-8", errors="replace") as f:
                proxyfile_snap = f.read()
        except OSError:
            proxyfile_snap = None

    digest = hashlib.md5(domain.encode()).hexdigest()  # nosec B324 — filename id only
    conf_path = os.path.join(proxy_dir, "%s_%s.conf" % (digest, domain))
    conf_existed = os.path.isfile(conf_path)
    conf_snap = None
    if conf_existed:
        try:
            with open(conf_path, encoding="utf-8", errors="replace") as f:
                conf_snap = f.read()
        except OSError:
            conf_snap = None

    def _rollback(err: str) -> Tuple[bool, str]:
        _restore_site_conf(domain, site_snap)
        if proxyfile_snap is not None:
            try:
                fs.atomic_write(_AAPANEL_PROXYFILE, proxyfile_snap, mode=0o644)
            except OSError:
                pass
        try:
            if conf_existed and conf_snap is not None:
                fs.atomic_write(conf_path, conf_snap, mode=0o644)
            elif os.path.isfile(conf_path) and not conf_existed:
                os.unlink(conf_path)
        except OSError:
            pass
        return False, err

    try:
        fs.atomic_write(conf_path, _proxy_conf_body(backend), mode=0o644)
    except OSError as e:
        return False, "cannot write proxy conf: %s" % e

    try:
        new_body = _ensure_site_proxy_include(site_snap, include_line, domain)
    except ValueError as e:
        return _rollback(str(e))
    if new_body != site_snap:
        try:
            fs.atomic_write(site_conf, new_body, mode=0o644)
        except OSError as e:
            return _rollback("cannot update site conf include: %s" % e)

    pf_ok, pf_err = _register_proxyfile(domain, int(port))
    if not pf_ok:
        return _rollback(pf_err)

    # Validate + reload nginx (same as CreateProxy would).
    nginx = "/www/server/nginx/sbin/nginx"
    if not os.path.isfile(nginx):
        nginx = "nginx"
    try:
        rc, out, err = run([nginx, "-t"], check=False, timeout=15)
        if rc != 0:
            detail = ((err or "") + (out or "")).strip()[-400:]
            return _rollback("nginx -t failed after proxy write: %s" % detail)
        run([nginx, "-s", "reload"], check=False, timeout=15)
    except Exception as e:
        return _rollback("nginx reload failed: %s" % e)
    return True, ""


def http_remove_site(domain: str) -> bool:
    """Delete a site via getData lookup + DeleteSite. Returns True on success."""
    row = _lookup_site_row(domain)
    if not isinstance(row, dict):
        return False
    site_id = row.get("id")
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

    Issues/installs a cert via apply_cert_api (best-effort), then requires a
    confirmed SetSSL / HttpToHttps / set_https_mode success. Cert-on-disk or
    apply_cert alone never counts as HTTPS enabled.
    """
    if not config.aapanel_api_key():
        return False
    nginx_err = require_nginx()
    if nginx_err:
        return False
    # Best-effort: place cert material first. Success here is NOT HTTPS enable.
    http_apply_cert(domain)
    for action, body in (
        ("SetSSL", {"type": "1", "siteName": domain, "updateOf": "1"}),
        ("HttpToHttps", {"siteName": domain}),
        ("set_https_mode", {"siteName": domain}),
    ):
        data, _ = http_request(
            "/site", params={"action": action}, body=body,
            method="POST", timeout=60)
        if isinstance(data, dict) and (data.get("status") or data.get("success")):
            return True
    return False


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
