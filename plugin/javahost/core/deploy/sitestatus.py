# coding: utf-8
"""
On-demand SSL/site status probe for a JavaHost reverse-proxy site.

Aggregates the cheap on-disk facts (instance health, stored domain, SSL marker)
with two OPTIONAL, bounded live probes that are too heavy for the list view:
  * cert parsing — `openssl x509 -enddate` on the live fullchain (openssl is
    always present); yields not_after / days_left / valid.
  * site reachability — bounded urllib HEAD/GET (timeout=3s) to http:// and
    https:// of the domain, reporting status code + whether http redirects to
    https.

Every boundary is wrapped: this module is defensive and NEVER raises. It is only
called from the GetSiteStatus endpoint (drawer/detail view), never from the
list_apps / health_all hot paths.
"""
from __future__ import annotations

import datetime
import os
import time
import urllib.error
import urllib.request
from typing import Dict, Optional

from . import proxy, ssl
from ..tomcat import instance
from ..util import shell, validate

_HTTP_TIMEOUT = 3.0


def _cert_info(domain: str) -> Optional[Dict]:
    """Parse the live LE cert for <domain> via openssl. Returns
    {exists, path, not_after, days_left, valid} or None when no cert file.

    Falls back to {exists:True, not_after:None,...} if openssl is missing or its
    output can't be parsed (the file is there, we just can't read the date)."""
    path = ssl._live_fullchain(domain)
    try:
        if not os.path.isfile(path):
            return None
    except Exception:
        return None
    info = {"exists": True, "path": path, "not_after": None,
            "days_left": None, "valid": None}
    try:
        # openssl: fixed argv, no shell; path is the plugin-known live cert file.
        rc, out, _ = shell.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", path],
            check=False, timeout=10)
        if rc != 0 or not out:
            return info
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("notAfter="):
                continue
            raw = line.split("=", 1)[1].strip()
            t = time.strptime(raw, "%b %d %H:%M:%S %Y %Z")
            dt = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
            info["not_after"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            now = datetime.datetime.now(datetime.timezone.utc)
            days = int((dt - now).total_seconds() // 86400)
            info["days_left"] = days
            info["valid"] = days >= 0
            break
    except Exception:
        # openssl missing / unparseable: keep the "exists but unknown" fallback
        return info
    return info


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that does NOT follow — so we can read the 3xx Location
    (to detect http->https redirects) instead of transparently following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe_http(domain: str) -> Dict:
    """HTTP probe: {code, redirects_to_https}. Does not follow redirects so a
    301 to https is observable. Bounded by _HTTP_TIMEOUT."""
    res = {"code": None, "redirects_to_https": None}
    url = "http://%s/" % domain
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        # URL scheme is a fixed http:// literal (validated domain); bounded timeout.
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
            res["code"] = resp.getcode()
            loc = resp.headers.get("Location") or ""
            res["redirects_to_https"] = loc.lower().startswith("https://")
    except urllib.error.HTTPError as e:
        res["code"] = e.code
        loc = ""
        try:
            loc = e.headers.get("Location") or ""
        except Exception:
            loc = ""
        res["redirects_to_https"] = loc.lower().startswith("https://")
    except Exception:
        pass
    return res


def _probe_https(domain: str) -> Dict:
    """HTTPS probe: {reachable, code}. Bounded by _HTTP_TIMEOUT."""
    res = {"reachable": False, "code": None}
    url = "https://%s/" % domain
    try:
        # URL scheme is a fixed https:// literal (validated domain); bounded timeout.
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            res["reachable"] = True
            res["code"] = resp.getcode()
    except urllib.error.HTTPError as e:
        res["reachable"] = True  # it answered TLS, just non-2xx
        res["code"] = e.code
    except Exception:
        pass
    return res


def _site_probe(domain: str) -> Dict:
    return {"http": _probe_http(domain), "https": _probe_https(domain)}


def probe(app: str, probe_site: bool = True) -> Dict:
    """Aggregate SSL/site status for <app>. Defensive: never raises.

    Returns:
      {app, health, domain, ssl_marker,
       cert: {exists,path,not_after,days_left,valid} | None,
       site: {http:{code,redirects_to_https}, https:{reachable,code}} | None}

    `cert` is None when no live cert file exists. `site` is None when probe_site
    is False or no domain is stored (nothing to reach)."""
    app = validate.identifier(app, "app")
    out = {
        "app": app,
        "health": None,
        "domain": None,
        "ssl_marker": False,
        "cert": None,
        "site": None,
    }
    try:
        out["health"] = instance.health(app)
    except Exception:
        out["health"] = None
    try:
        out["domain"] = proxy.read_domain(app)
    except Exception:
        out["domain"] = None
    try:
        out["ssl_marker"] = ssl.read_ssl(app)
    except Exception:
        out["ssl_marker"] = False

    domain = out["domain"]
    if domain:
        try:
            out["cert"] = _cert_info(domain)
        except Exception:
            out["cert"] = None
        if probe_site:
            try:
                out["site"] = _site_probe(domain)
            except Exception:
                out["site"] = None
    return out
