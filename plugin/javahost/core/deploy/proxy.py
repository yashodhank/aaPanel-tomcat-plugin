# coding: utf-8
"""
Reverse-proxy helper. Generates a plugin-owned Nginx vhost pointing at a
Tomcat instance's loopback HTTP port. Never edits another plugin's config
(closes F8) — writes only into a JavaHost-owned include dir and validates with
`nginx -t` before asking the panel to reload.

Site registration (aapanel_add_site) tries three paths in order:
  1. aaPanel's native `site.AddSite()` API (modern path — /www/server/panel/class/site.py)
  2. aaPanel's legacy `panelSite` module (older aaPanel versions)
  3. aaPanel HTTP API (POST /site?action=AddSite, loopback)
"""
from __future__ import annotations

import os
import re
from typing import Dict, Optional

from .. import config
from ..util import shell, fs, validate

VHOST_DIR = "/www/server/javahost/vhost/nginx"
NGINX_CONF = "/www/server/nginx/conf/nginx.conf"
# Shared ACME webroot for HTTP-01 challenge files (issuance + renewal). Both the
# native (aaPanel) and certbot paths serve challenges from here, so the port-80
# server ALWAYS exposes /.well-known/acme-challenge/ pointing at it.
ACME_WEBROOT = "/www/wwwroot/acme"
# aaPanel's panel class path.
AAPANEL_PANEL_CLASS = "/www/server/panel/class"
# Public-domain suffix for default <app>.<suffix> domains is read LIVE from
# config.site_suffix() at call time (see default_domain) — never cached at import
# (a module-level read would freeze a stale value for the process lifetime).

# The ACME challenge location is present in BOTH http-only and https vhosts so a
# cert can be issued AND auto-renewed without ever taking the site down.
_ACME_LOCATION = """    location ^~ /.well-known/acme-challenge/ {
        root @@acme@@;
        default_type "text/plain";
        try_files $uri =404;
    }"""

# HTTP-only vhost: proxy everything to the backend + serve ACME challenges.
_TEMPLATE = """# Managed by JavaHost — instance @@app@@ (@@domain@@). Do not edit by hand.
server {
    listen 80;
    listen [::]:80;
    server_name @@domain@@;
@@acme_location@@
    location / {
        proxy_pass http://127.0.0.1:@@port@@;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_read_timeout 300;
    }
}
"""

# HTTPS vhost: port-80 server serves only ACME + redirects to https; the 443
# server terminates TLS and proxies to the backend.
_TEMPLATE_SSL = """# Managed by JavaHost — instance @@app@@ (@@domain@@) [SSL]. Do not edit by hand.
server {
    listen 80;
    listen [::]:80;
    server_name @@domain@@;
@@acme_location@@
    location / {
        return 301 https://$host$request_uri;
    }
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name @@domain@@;
    ssl_certificate /etc/letsencrypt/live/@@domain@@/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/@@domain@@/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:@@port@@;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_read_timeout 300;
    }
}
"""


def vhost_path(app: str) -> str:
    return os.path.join(VHOST_DIR, "%s.conf" % app)


def write_vhost(app: str, domain: str, port: int, ssl: bool = False) -> str:
    """Render the plugin-owned nginx vhost for <app>.

    ssl=False (default, keeps old 2/3-arg callers working): an HTTP server that
    proxies / to the backend AND serves the ACME challenge location.
    ssl=True: the HTTP server serves the ACME location and 301-redirects to
    https; a 443 server terminates TLS (LE live cert) and proxies to the backend.
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    fs.ensure_dir(VHOST_DIR)
    acme = _ACME_LOCATION.replace("@@acme@@", ACME_WEBROOT)
    template = _TEMPLATE_SSL if ssl else _TEMPLATE
    body = (template
            .replace("@@acme_location@@", acme)
            .replace("@@app@@", app)
            .replace("@@domain@@", domain)
            .replace("@@port@@", str(port)))
    path = vhost_path(app)
    fs.atomic_write(path, body, mode=0o644)
    return path


def remove_vhost(app: str) -> None:
    app = validate.identifier(app, "app")
    p = vhost_path(app)
    if os.path.exists(p):
        os.unlink(p)


def nginx_test() -> bool:
    nginx = shell.which("nginx") or "/www/server/nginx/sbin/nginx"
    rc, _, _ = shell.run([nginx, "-t"], check=False)
    return rc == 0


def include_hint() -> str:
    """Line the user adds once to nginx.conf http{} to pick up JavaHost vhosts."""
    return "include %s/*.conf;" % VHOST_DIR


def default_domain(app: str) -> Optional[str]:
    """Convention domain for an app: "<app>.<site_suffix>" when a suffix is
    configured, else None (no FQDN is ever guessed). Reads the suffix live from
    config so a config edit takes effect without a reload."""
    app = validate.identifier(app, "app")
    suffix = config.site_suffix()
    if not suffix:
        return None
    return validate.domain("%s.%s" % (app, suffix))


_INCLUDE_LINE = "include %s/*.conf;" % VHOST_DIR


def ensure_include(nginx_conf: str = NGINX_CONF) -> bool:
    """Idempotently add our vhost include into nginx's http{} block.

    Returns True if the include was added, False if it was already present (or
    the conf is unavailable). Inserts immediately after the opening `http {` so
    the directive lands inside the http context. Never touches other plugins'
    server blocks — only this one include line.
    """
    if not os.path.isfile(nginx_conf):
        return False
    with open(nginx_conf, encoding="utf-8", errors="replace") as f:
        content = f.read()
    if VHOST_DIR in content and "include" in content:
        # already referenced (idempotent — tolerate trailing-slash variants)
        if _INCLUDE_LINE in content or ("%s/*.conf" % VHOST_DIR) in content:
            return False
    # Locate the http{} block by a real `http {` token (not a bare substring
    # match, which would also hit comments, "https", $http_host, etc.).
    m = re.search(r"\bhttp\s*\{", content)
    if not m:
        return False
    brace = content.index("{", m.start())
    injected = (content[: brace + 1]
                + "\n    " + _INCLUDE_LINE + "\n"
                + content[brace + 1:])
    fs.atomic_write(nginx_conf, injected, mode=0o644)
    # Validate the rewritten config; if nginx rejects it, restore the original
    # so we never leave nginx in a non-reloadable state.
    if not nginx_test():
        fs.atomic_write(nginx_conf, content, mode=0o644)
        return False
    return True


def reload_nginx() -> bool:
    """Validate config (`nginx -t`) then graceful reload (`nginx -s reload`).

    Returns True on a successful test+reload, False otherwise. Never raises so a
    site write can report partial success rather than 500."""
    nginx = shell.which("nginx") or "/www/server/nginx/sbin/nginx"
    rc, _, _ = shell.run([nginx, "-t"], check=False)
    if rc != 0:
        return False
    rc, _, _ = shell.run([nginx, "-s", "reload"], check=False)
    return rc == 0


# --------------------------------------------------------------------------- #
# aaPanel site registration (multi-path)
# --------------------------------------------------------------------------- #

def _try_aapanel_class_api(domain: str, port: int) -> Optional[Dict]:
    """Path 1: modern aaPanel (site.AddSite with web_type=proxy).

    Imports the `site` class from /www/server/panel/class/site.py and calls
    AddSite() with a proxy configuration. Returns a success dict or None on
    any failure (caller falls through to the next path).
    """
    import sys
    panel_class = AAPANEL_PANEL_CLASS
    if panel_class not in sys.path:
        sys.path.insert(0, panel_class)

    try:
        from site import site as panelSite  # noqa: F401
    except Exception:
        return None

    try:
        site_obj = panelSite()

        class _G(object):
            pass
        g = _G()
        g.webname = {"domain": domain, "domainlist": [], "count": 0}
        g.type = "proxy"
        g.port = 80
        g.ps = "JavaHost: %s -> 127.0.0.1:%d" % (domain, port)
        g.path = "/www/wwwroot/%s" % domain
        g.version = "00"

        # Primary API: AddSite (modern aaPanel 7.x / 8.x)
        res = site_obj.AddSite(g)
        if isinstance(res, dict) and res.get("status"):
            return {"ok": True, "path": "aapanel", "detail": "via site.AddSite"}

        # Fallback methods for older aaPanel versions
        for meth in ("add_redirect", "AddProxy", "create_proxy", "set_proxy"):
            fn = getattr(site_obj, meth, None)
            if not callable(fn):
                continue
            res = fn(g)
            if isinstance(res, dict):
                if res.get("status"):
                    return {"ok": True, "path": "aapanel",
                            "detail": "via site.%s" % meth}
                continue
            return {"ok": True, "path": "aapanel",
                    "detail": "via site.%s" % meth}
        return None
    except Exception:
        return None


def _try_legacy_panelSite_import(domain: str, port: int) -> Optional[Dict]:
    """Path 2: legacy aaPanel `panelSite` module (older aaPanel versions)."""
    try:
        import panelSite  # noqa: F401
    except Exception:
        return None

    try:
        site = panelSite.panelSite() if hasattr(panelSite, "panelSite") else panelSite

        class _G(object):
            pass
        g = _G()
        g.sitename = domain
        g.domain = domain
        g.proxyname = domain
        g.proxysite = "http://127.0.0.1:%d" % port
        g.todomain = "http://127.0.0.1:%d" % port
        g.type = "1"
        g.port = "80"

        for meth in ("add_redirect", "AddProxy", "create_proxy", "set_proxy"):
            fn = getattr(site, meth, None)
            if not callable(fn):
                continue
            res = fn(g)
            if isinstance(res, dict):
                if res.get("status"):
                    return {"ok": True, "path": "aapanel",
                            "detail": "via panelSite.%s" % meth}
                continue
            return {"ok": True, "path": "aapanel",
                    "detail": "via panelSite.%s" % meth}
        return None
    except Exception:
        return None


def _try_aapanel_http_api(domain: str, port: int) -> Optional[Dict]:
    """Path 3: aaPanel's loopback HTTP API for site creation.

    Calls POST /site?action=AddSite with the same auth scheme already
    proven in ssl.py (_aapanel_apply). Returns a success dict or None
    when api_sk is unset or the call fails (caller falls through).
    """
    api_sk = config.aapanel_api_key()
    if not api_sk:
        return None
    try:
        import hashlib
        import json
        import time
        import urllib.parse
        import urllib.request
        import ssl as _sslmod

        panel_port = config.aapanel_port()
        request_time = int(time.time())
        sk_md5 = hashlib.md5(api_sk.encode()).hexdigest()  # nosec B324
        token = hashlib.md5(
            (str(request_time) + sk_md5).encode()
        ).hexdigest()  # nosec B324

        query = urllib.parse.urlencode({
            "action": "AddSite",
            "request_time": str(request_time),
            "request_token": token,
        })
        url = "https://127.0.0.1:%d/site?%s" % (panel_port, query)

        body = urllib.parse.urlencode({
            "webname": json.dumps({"domain": domain, "domainlist": [], "count": 0}),
            "path": "/www/wwwroot/%s" % domain,
            "type": "proxy",
            "type_id": "0",
            "version": "00",
            "port": "80",
            "ps": "JavaHost: %s -> 127.0.0.1:%d" % (domain, port),
            "ftp": "false",
            "sql": "false",
        }).encode()

        ctx = _sslmod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _sslmod.CERT_NONE
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if isinstance(data, dict) and (data.get("status") or data.get("siteStatus")):
            return {"ok": True, "path": "aapanel-http",
                    "detail": "via HTTP AddSite"}
        return None
    except Exception:
        return None


def _aapanel_http_remove_site(domain: str) -> bool:
    """Remove a site via aaPanel's HTTP API.

    Queries the site ID from GET /data?action=getData&table=sites,
    then calls POST /site?action=DeleteSite. Returns True if deleted.
    """
    api_sk = config.aapanel_api_key()
    if not api_sk:
        return False
    try:
        import hashlib
        import json
        import time
        import urllib.parse
        import urllib.request
        import ssl as _sslmod

        panel_port = config.aapanel_port()
        request_time = int(time.time())
        sk_md5 = hashlib.md5(api_sk.encode()).hexdigest()  # nosec B324
        token = hashlib.md5(
            (str(request_time) + sk_md5).encode()
        ).hexdigest()  # nosec B324

        ctx = _sslmod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _sslmod.CERT_NONE

        # Step 1: look up the site ID
        qs = urllib.parse.urlencode({
            "action": "getData",
            "table": "sites",
            "search": domain,
            "request_time": str(request_time),
            "request_token": token,
        })
        url = "https://127.0.0.1:%d/data?%s" % (panel_port, qs)
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
        result = json.loads(raw)
        site_id = None
        if isinstance(result, dict):
            data_list = result.get("data") or result.get("msg") or []
            if isinstance(data_list, list):
                for row in data_list:
                    if isinstance(row, dict) and row.get("name") == domain:
                        site_id = row.get("id")
                        break
        if not site_id:
            return False

        # Step 2: delete the site
        request_time2 = int(time.time())
        token2 = hashlib.md5(
            (str(request_time2) + sk_md5).encode()
        ).hexdigest()  # nosec B324

        qs2 = urllib.parse.urlencode({
            "action": "DeleteSite",
            "request_time": str(request_time2),
            "request_token": token2,
        })
        url2 = "https://127.0.0.1:%d/site?%s" % (panel_port, qs2)
        body = urllib.parse.urlencode({
            "id": str(site_id),
            "webname": domain,
        }).encode()
        req2 = urllib.request.Request(url2, data=body, method="POST")
        with urllib.request.urlopen(req2, timeout=30, context=ctx) as resp2:  # noqa: S310
            raw2 = resp2.read().decode("utf-8", "replace")
        data2 = json.loads(raw2)
        if isinstance(data2, dict) and data2.get("status"):
            return True
        return False
    except Exception:
        return False


def aapanel_add_site(domain: str, port: int) -> Dict:
    """Register a reverse-proxy site via aaPanel's native API, 3-tier fallback.

    Tries in order:
      1. aaPanel site.AddSite() (modern — /www/server/panel/class/site.py)
      2. Legacy panelSite module (older aaPanel versions)
      3. aaPanel HTTP API (POST /site?action=AddSite, loopback)

    Returns {"ok": bool, "path": "aapanel"|"aapanel-http", "detail": str}.
    Any failure is swallowed so callers can return an error to the user.
    """
    domain = validate.domain(domain)
    port = validate.port(port)

    # Path 1: modern aaPanel class API
    res = _try_aapanel_class_api(domain, port)
    if res is not None:
        return res

    # Path 2: legacy panelSite module
    res = _try_legacy_panelSite_import(domain, port)
    if res is not None:
        return res

    # Path 3: HTTP API
    res = _try_aapanel_http_api(domain, port)
    if res is not None:
        return res

    return {"ok": False, "path": "aapanel",
            "detail": "aaPanel site registration failed: all 3 API paths exhausted"}


def aapanel_remove_site(domain: str) -> bool:
    """Remove a site from aaPanel records (HTTP API first, then class API).

    Returns True if site was found and removed from any aaPanel path, False if
    it was not registered in aaPanel at all.
    """
    domain = validate.domain(domain)
    removed = False

    # Path 1: HTTP API (most reliable — uses aaPanel's REST layer)
    removed = _aapanel_http_remove_site(domain)

    # Path 2: modern aaPanel class API
    if not removed:
        try:
            import sys
            panel_class = AAPANEL_PANEL_CLASS
            if panel_class not in sys.path:
                sys.path.insert(0, panel_class)
            from site import site as panelSite  # noqa: F401
            site_obj = panelSite()

            class _G(object):
                pass
            g = _G()
            g.webname = domain
            g.id = None

            if hasattr(site_obj, "DeleteSite"):
                res = site_obj.DeleteSite(g)
                if isinstance(res, dict) and res.get("status"):
                    removed = True
        except Exception:
            pass

    # Path 3: legacy panelSite
    if not removed:
        try:
            import panelSite  # noqa: F401
            site = (panelSite.panelSite()
                    if hasattr(panelSite, "panelSite") else panelSite)
            if hasattr(site, "DeleteSite"):
                class _G(object):
                    pass
                g = _G()
                g.domain = domain
                g.webname = domain
                res = site.DeleteSite(g)
                if isinstance(res, dict) and res.get("status"):
                    removed = True
        except Exception:
            pass

    return removed


def _site_marker(app: str) -> str:
    """Per-instance marker recording the chosen public domain (read by list_apps).

    Lives at <INSTANCE_ROOT>/<app>/bin/site.domain. Imported lazily to avoid a
    proxy<->instance import cycle."""
    from ..tomcat import instance
    app = validate.identifier(app, "app")
    return os.path.join(instance.base_path(app), "bin", "site.domain")


def _store_domain(app: str, domain: str) -> None:
    path = _site_marker(app)
    fs.ensure_dir(os.path.dirname(path))
    fs.atomic_write(path, domain + "\n", mode=0o644)


def _clear_domain(app: str) -> None:
    try:
        path = _site_marker(app)
    except Exception:
        return
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def read_domain(app: str) -> Optional[str]:
    """Stored public domain for an app, or None. Defensive: never raises."""
    try:
        path = _site_marker(app)
    except Exception:
        return None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = f.read().strip()
        return d or None
    except OSError:
        return None


def set_site(app: str, domain: str, port: int) -> Dict:
    """Publish <app> at <domain> -> http://127.0.0.1:<port>.

    Registers the site through aaPanel's native API (3-tier fallback). On
    failure, returns an error — no nginx vhost fallback. The chosen domain is
    recorded so list_apps() can surface it.
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)

    aap = aapanel_add_site(domain, port)
    if not aap.get("ok"):
        msg = "aaPanel site registration failed: %s. " \
              "Ensure the plugin has a valid aapanel_api_key in config " \
              "and aaPanel's panel is running." % aap.get("detail", "unknown error")
        return {"ok": False, "error": msg, "detail": aap.get("detail", "")}

    # The include line must exist: even when aaPanel "owns" the site, a later
    # SSL flip or manual edit may rely on JavaHost vhosts being picked up.
    # ensure_include() is idempotent and self-validating.
    ensure_include()

    _store_domain(app, domain)
    return {"ok": True, "domain": domain, "url": "http://%s/" % domain,
            "via": "aapanel", "aapanel": aap.get("detail", "")}


def remove_site(app: str) -> Dict:
    """Remove the app's vhost, aaPanel site record, and reload nginx; clear the
    stored domain marker. Tries aaPanel API first, then nginx orphan cleanup."""
    app = validate.identifier(app, "app")
    domain = read_domain(app)

    # Try to remove from aaPanel (API + HTTP)
    aapanel_removed = False
    if domain:
        aapanel_removed = aapanel_remove_site(domain)

    remove_vhost(app)
    reload_nginx()
    _clear_domain(app)
    return {"app": app, "removed": True, "aapanel_cleaned": aapanel_removed}
