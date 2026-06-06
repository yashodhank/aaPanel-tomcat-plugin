# coding: utf-8
"""
Reverse-proxy helper. Generates a plugin-owned Nginx vhost pointing at a
Tomcat instance's loopback HTTP port. Never edits another plugin's config
(closes F8) — writes only into a JavaHost-owned include dir and validates with
`nginx -t` before asking the panel to reload.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from ..util import shell, fs, validate

VHOST_DIR = "/www/server/javahost/vhost/nginx"
NGINX_CONF = "/www/server/nginx/conf/nginx.conf"
# Default public domain convention: <app>.5d.bisotech.in
SITE_SUFFIX = "5d.bisotech.in"

_TEMPLATE = """# Managed by JavaHost — instance @@app@@ ($domain). Do not edit by hand.
server {
    listen 80;
    server_name @@domain@@;
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


def vhost_path(app: str) -> str:
    return os.path.join(VHOST_DIR, "%s.conf" % app)


def write_vhost(app: str, domain: str, port: int) -> str:
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    fs.ensure_dir(VHOST_DIR)
    body = (_TEMPLATE
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


def default_domain(app: str) -> str:
    """Convention domain for an app: <app>.5d.bisotech.in."""
    app = validate.identifier(app, "app")
    return validate.domain("%s.%s" % (app, SITE_SUFFIX))


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
    idx = content.find("http")
    # find the opening brace of the http{} block
    brace = -1
    while idx != -1:
        b = content.find("{", idx)
        if b != -1:
            brace = b
            break
        idx = content.find("http", idx + 4)
    if brace == -1:
        return False
    injected = (content[: brace + 1]
                + "\n    " + _INCLUDE_LINE + "\n"
                + content[brace + 1:])
    fs.atomic_write(nginx_conf, injected, mode=0o644)
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


def aapanel_add_site(domain: str, port: int) -> Dict:
    """Best-effort: register a reverse-proxy site via aaPanel's native site API.

    Imported lazily at runtime (the panel ships `panelSite`); any import/attr/call
    failure is swallowed so callers can fall back to the nginx-vhost path. Returns
    {"ok": bool, "path": "aapanel"|..., "detail": str}.
    """
    domain = validate.domain(domain)
    port = validate.port(port)
    try:
        import panelSite  # provided by the aaPanel runtime  # noqa: F401
    except Exception as e:
        return {"ok": False, "path": "aapanel", "detail": "panelSite import failed: %s" % e}
    try:
        site = panelSite.panelSite() if hasattr(panelSite, "panelSite") else panelSite
        # aaPanel's reverse-proxy/add-site signatures vary across versions; try the
        # documented proxy method first, then a generic add-site, via a tiny shim
        # `get` object carrying the params aaPanel expects.
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
            if callable(fn):
                fn(g)
                return {"ok": True, "path": "aapanel", "detail": "via panelSite.%s" % meth}
        return {"ok": False, "path": "aapanel", "detail": "no usable panelSite proxy method"}
    except Exception as e:
        return {"ok": False, "path": "aapanel", "detail": "panelSite call failed: %s" % e}


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

    Primary path: aaPanel's native site API. On ANY failure there, falls back to
    our own clean nginx vhost (write_vhost + ensure_include + reload_nginx). The
    chosen domain is recorded so list_apps() can surface it. The result documents
    which path actually ran via "via".
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)

    aap = aapanel_add_site(domain, port)
    via = "aapanel"
    if not aap.get("ok"):
        # fallback: plugin-owned nginx vhost (never edits other plugins' config)
        write_vhost(app, domain, port)
        ensure_include()
        reload_nginx()
        via = "nginx-vhost"

    _store_domain(app, domain)
    return {"domain": domain, "url": "http://%s/" % domain, "via": via,
            "aapanel": aap.get("detail", "")}


def remove_site(app: str) -> Dict:
    """Remove the app's vhost and reload nginx; clear the stored domain marker."""
    app = validate.identifier(app, "app")
    remove_vhost(app)
    reload_nginx()
    _clear_domain(app)
    return {"app": app, "removed": True}
