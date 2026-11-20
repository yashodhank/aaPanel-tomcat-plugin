# coding: utf-8
"""
Reverse-proxy helper. Publishes an app at a domain in one of two ways
(config "publish_mode"):

  vhost (default)  one JavaHost-owned file, javahost_<app>.conf, in the panel's
                   nginx vhost directory. nginx.conf already loads that
                   directory, so nginx.conf itself is never edited, and no panel
                   API key is needed. The file is kept only if `nginx -t`
                   accepts it.
  panel            a website registered in the panel (needs the panel API key).

Panel registration (aapanel_add_site) tries three paths in order:
  1. aaPanel's native `site.AddSite()` API (modern path — /www/server/panel/class/site.py)
  2. aaPanel's legacy `panelSite` module (older aaPanel versions)
  3. aaPanel HTTP API (POST /site?action=AddSite, loopback)
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
from typing import Dict, Optional

from .. import config
from ..compat import aapanel as panel_api
from ..util import shell, fs, validate

# The panel's own vhost directory: its nginx.conf includes *.conf from here.
VHOST_DIR = "/www/server/panel/vhost/nginx"
# Where vhosts lived before 0.30; it needed an include line added to nginx.conf.
LEGACY_VHOST_DIR = "/www/server/javahost/vhost/nginx"
VHOST_PREFIX = "javahost_"
NGINX_CONF = "/www/server/nginx/conf/nginx.conf"
PANEL_NGINX = "/www/server/nginx/sbin/nginx"
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
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 300;
    }
}
"""

# HTTPS vhost: port-80 server serves only ACME + redirects to https; the 443
# server terminates TLS and proxies to the backend.
# @@http2_line@@ is either "    http2 on;\n" (nginx ≥1.25.1) or "" with
# listen lines carrying the legacy "http2" flag (older aaPanel nginx).
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
    listen @@listen_443@@;
    listen [::]:@@listen_443_v6@@;
@@http2_line@@    server_name @@domain@@;
    ssl_certificate /etc/letsencrypt/live/@@cert_domain@@/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/@@cert_domain@@/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:@@port@@;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 300;
    }
}
"""

def _nginx() -> str:
    """The panel's own nginx binary. A distro nginx package can sit earlier on
    PATH with a config and pid file of its own, so PATH is only the fallback."""
    if os.path.isfile(PANEL_NGINX):
        return PANEL_NGINX
    return shell.which("nginx") or PANEL_NGINX


def _nginx_supports_http2_on() -> bool:
    """True when `nginx -V` reports a version that accepts standalone `http2 on;`."""
    nginx = _nginx()
    try:
        rc, out, err = shell.run([nginx, "-V"], check=False)
    except Exception:
        return False
    blob = (out or "") + (err or "")
    m = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", blob)
    if not m:
        return False
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # http2 on; directive: nginx 1.25.1+
    return (major, minor, patch) >= (1, 25, 1)


def vhost_path(app: str) -> str:
    return os.path.join(VHOST_DIR, "%s%s.conf" % (VHOST_PREFIX, app))


def legacy_vhost_path(app: str) -> str:
    return os.path.join(LEGACY_VHOST_DIR, "%s.conf" % app)


def vhost_apps() -> list:
    """Apps that have a JavaHost vhost file, in either location."""
    apps = set()
    for vdir, prefix in ((VHOST_DIR, VHOST_PREFIX), (LEGACY_VHOST_DIR, "")):
        try:
            names = os.listdir(vdir)
        except OSError:
            continue
        for f in names:
            if (f.startswith(prefix) and f.endswith(".conf")
                    and (not prefix or _is_ours(os.path.join(vdir, f)))):
                apps.add(f[len(prefix):-len(".conf")])
    return sorted(apps)


def _is_ours(path: str) -> bool:
    """A file in the panel's vhost folder is JavaHost's only if JavaHost wrote
    its header; the name alone could belong to a panel website."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(64).startswith("# Managed by JavaHost")
    except OSError:
        return False


@contextlib.contextmanager
def _publish_lock():
    """One publish at a time: check, write, test and roll back as a unit."""
    os.makedirs(VHOST_DIR, exist_ok=True)
    fd = os.open(VHOST_DIR, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_vhost(app: str, domain: str, port: int, ssl: bool = False,
                cert_domain: Optional[str] = None,
                wildcard_name: Optional[str] = None) -> str:
    """Render the plugin-owned nginx vhost for <app>.

    ssl=False (default, keeps old 2/3-arg callers working): an HTTP server that
    proxies / to the backend AND serves the ACME challenge location.
    ssl=True: the HTTP server serves the ACME location and 301-redirects to
    https; a 443 server terminates TLS (LE live cert) and proxies to the backend.

    cert_domain (optional): domain whose LE cert dir to use. When set, cert
    paths are /etc/letsencrypt/live/<cert_domain>/ instead of <domain>/.
    Used for wildcard certs where the cert lives at the base domain path.

    wildcard_name is accepted for API compatibility but is NEVER added to
    server_name (doing so would hijack sibling hosts under the same wildcard).
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    # not fs.ensure_dir: it would chmod a directory the panel owns
    os.makedirs(VHOST_DIR, exist_ok=True)
    acme = _ACME_LOCATION.replace("@@acme@@", ACME_WEBROOT)
    cert_for = cert_domain or domain
    # Intentionally ignore wildcard_name for server_name — cert reuse only.
    _ = wildcard_name
    template = _TEMPLATE_SSL if ssl else _TEMPLATE
    if ssl and _nginx_supports_http2_on():
        listen_443, listen_443_v6, http2_line = "443 ssl", "443 ssl", "    http2 on;\n"
    elif ssl:
        listen_443, listen_443_v6, http2_line = "443 ssl http2", "443 ssl http2", ""
    else:
        listen_443, listen_443_v6, http2_line = "443 ssl", "443 ssl", ""
    body = (template
            .replace("@@acme_location@@", acme)
            .replace("@@app@@", app)
            .replace("@@cert_domain@@", cert_for)  # cert at base domain path
            .replace("@@domain@@", domain)
            .replace("@@port@@", str(port))
            .replace("@@listen_443@@", listen_443)
            .replace("@@listen_443_v6@@", listen_443_v6)
            .replace("@@http2_line@@", http2_line))
    path = vhost_path(app)
    fs.atomic_write(path, body, mode=0o644)
    # one file per app: drop a pre-0.30 copy so the domain is not defined twice
    legacy = legacy_vhost_path(app)
    if os.path.exists(legacy):
        os.unlink(legacy)
    return path


def remove_vhost(app: str) -> None:
    app = validate.identifier(app, "app")
    p = vhost_path(app)
    if os.path.exists(p) and _is_ours(p):
        os.unlink(p)
    p = legacy_vhost_path(app)
    if os.path.exists(p):
        os.unlink(p)


def publish_vhost(app: str, domain: str, port: int, **kw) -> Optional[str]:
    """Write the app's vhost and reload nginx. Returns an error, or None.

    A file nginx rejects would block every later reload on the server, the
    panel's own included, so on any failure the previous files are put back."""
    app = validate.identifier(app, "app")
    path = vhost_path(app)
    with _publish_lock():
        if os.path.exists(path) and not _is_ours(path):
            return "%s exists and was not written by JavaHost" % path
        # Every writer goes through here, so this is where a domain that a panel
        # website or another app already serves is refused.
        clash = _domain_in_use(app, validate.domain(domain))
        if clash:
            return clash
        before = {}
        for p in (path, legacy_vhost_path(app)):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    before[p] = f.read()
            except OSError:
                pass
        try:
            write_vhost(app, domain, port, **kw)
            if not ensure_include():
                err = "nginx.conf does not load %s/*.conf" % VHOST_DIR
            elif not reload_nginx():
                err = ("nginx did not accept or reload the configuration "
                       "(run `nginx -t` for the reason)")
            else:
                return None
        except Exception as e:
            err = str(e)
        remove_vhost(app)
        for p, body in before.items():
            fs.atomic_write(p, body, mode=0o644)
        return err + "; the site file was not kept"


def nginx_test() -> bool:
    nginx = _nginx()
    rc, _, _ = shell.run([nginx, "-t"], check=False)
    return rc == 0


def include_hint() -> str:
    """The nginx.conf line that loads JavaHost vhosts (the panel ships it)."""
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


def ensure_include(nginx_conf: Optional[str] = None) -> bool:
    """True when nginx.conf loads VHOST_DIR. JavaHost no longer edits
    nginx.conf: the panel's own nginx.conf already includes its vhost folder."""
    try:
        with open(nginx_conf or NGINX_CONF, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False
    return bool(re.search(r"^\s*include\s+%s/\*\.conf\s*;" % re.escape(VHOST_DIR),
                          content, re.M))


def reload_nginx() -> bool:
    """Validate config (`nginx -t`) then graceful reload (`nginx -s reload`).

    Returns True on a successful test+reload, False otherwise. Never raises so a
    site write can report partial success rather than 500."""
    nginx = _nginx()
    rc, _, _ = shell.run([nginx, "-t"], check=False)
    if rc != 0:
        return False
    rc, _, _ = shell.run([nginx, "-s", "reload"], check=False)
    return rc == 0


# --------------------------------------------------------------------------- #
# aaPanel site registration (multi-path)
# --------------------------------------------------------------------------- #

def _try_aapanel_class_api(domain: str, port: int) -> Optional[Dict]:
    """Path 1: aaPanel's panelSite class API (primary, no api_sk needed).

    Imports `panelSite` from /www/server/panel/class/panelSite.py and calls
    CreateProxy() with the correct parameters. This is the SAME module aaPanel's
    own UI uses. The class name and file name are both `panelSite`.
    """
    import sys
    panel_class = AAPANEL_PANEL_CLASS
    if panel_class not in sys.path:
        sys.path.insert(0, panel_class)

    try:
        from panelSite import panelSite as _SiteClass  # noqa: F401
    except Exception:
        return None

    try:
        site_obj = _SiteClass()
        backend = "http://127.0.0.1:%d" % port

        class _G(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

        g = _G()
        g["proxyname"] = "javahost-" + domain.replace(".", "-")[:40]
        g["sitename"] = domain
        g["proxydir"] = "/"
        g["proxysite"] = backend
        g["todomain"] = "$host"
        # panelSite.CreateProxy expects string fields (int values crash/fail checks).
        g["type"] = "1"
        g["cache"] = "0"
        g["subfilter"] = "[]"
        g["advanced"] = "0"
        g["cachetime"] = "1"
        g["nocheck"] = "1"

        # Ensure proxy conf dir exists (CreateProxy writes here; missing => silent fail).
        try:
            os.makedirs("/www/server/panel/vhost/nginx/proxy", mode=0o755, exist_ok=True)
        except OSError:
            pass

        # CreateProxy is the dedicated aaPanel method for reverse-proxy sites
        res = site_obj.CreateProxy(g)
        if isinstance(res, dict) and (res.get("status") or res.get("siteStatus")):
            return {"ok": True, "path": "aapanel",
                    "detail": "via panelSite.CreateProxy"}
        return None
    except Exception:
        return None


def _try_legacy_panelSite_import(domain: str, port: int) -> Optional[Dict]:
    """Path 2: legacy `import panelSite` style (older aaPanel versions where
    the module isn't on the python path it in but can be resolved)."""

    try:
        import panelSite  # noqa: F401
    except Exception:
        return None

    try:
        site = (panelSite.panelSite()
                if hasattr(panelSite, "panelSite") else panelSite)
        backend = "http://127.0.0.1:%d" % port

        class _G(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

        g = _G()
        g["proxyname"] = "javahost-" + domain.replace(".", "-")[:40]
        g["sitename"] = domain
        g["proxydir"] = "/"
        g["proxysite"] = backend
        g["todomain"] = "$host"
        g["type"] = "1"
        g["cache"] = "0"
        g["subfilter"] = "[]"
        g["advanced"] = "0"
        g["cachetime"] = "1"
        g["nocheck"] = "1"

        try:
            os.makedirs("/www/server/panel/vhost/nginx/proxy", mode=0o755, exist_ok=True)
        except OSError:
            pass

        if hasattr(site, "CreateProxy"):
            res = site.CreateProxy(g)
            if isinstance(res, dict) and (res.get("status") or res.get("siteStatus")):
                return {"ok": True, "path": "aapanel",
                        "detail": "via panelSite.CreateProxy"}

        # Older versions may have these fallback method names
        for meth in ("AddProxy", "set_proxy", "add_redirect", "create_proxy"):
            fn = getattr(site, meth, None)
            if not callable(fn):
                continue
            # Build a simpler _G object for older methods
            class _G2(object):
                pass
            g2 = _G2()
            g2.sitename = domain
            g2.proxyname = domain
            g2.proxysite = backend
            g2.todomain = backend
            g2.type = "1"
            g2.port = "80"
            res = fn(g2)
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
    """Path 3: aaPanel loopback HTTP API — AddSite then CreateProxy with upstream.

    AddSite alone does not wire proxysite on typical builds; CreateProxy must
    follow. Auth + HTTP live in compat.aapanel (sole panel-coupling module).
    """
    if not panel_api.api_key_configured():
        return None
    try:
        res = panel_api.http_add_site_with_proxy(domain, port)
        return res
    except Exception:
        return None


def _aapanel_http_remove_site(domain: str) -> bool:
    """Remove a site via aaPanel's HTTP API (compat helper)."""
    try:
        return bool(panel_api.http_remove_site(domain))
    except Exception:
        return False


def aapanel_add_site(domain: str, port: int) -> Dict:
    """Register a reverse-proxy site via aaPanel's native API, 3-tier fallback.

    Tries in order:
      1. aaPanel HTTP API (POST /site?action=AddSite, loopback; requires api_sk)
      2. aaPanel panelSite.CreateProxy() (native Python, may crash on some versions)
      3. Legacy panelSite module (older aaPanel versions)

    Returns {"ok": bool, "path": "aapanel"|"aapanel-http", "detail": str,
            "tried": [str]}.
    """
    domain = validate.domain(domain)
    port = validate.port(port)
    tried = []

    # Path 1: HTTP API (most reliable — same auth scheme proven on VPS)
    if config.aapanel_api_key():
        res = _try_aapanel_http_api(domain, port)
        tried.append("http-api")
        if res is not None:
            # Success OR a diagnosed AddSite-ok/CreateProxy-fail — stop falling
            # through (further paths cannot fix a half-created site cleanly).
            return res
    else:
        tried.append("http-api-skipped-no-key")

    # Path 2: modern aaPanel class API (panelSite.CreateProxy)
    res = _try_aapanel_class_api(domain, port)
    tried.append("class-api")
    if res is not None:
        return res

    # Path 3: legacy panelSite module (older versions)
    res = _try_legacy_panelSite_import(domain, port)
    tried.append("legacy-panelsite")
    if res is not None:
        return res

    paths = ", ".join(tried)
    return {"ok": False, "path": "aapanel",
            "detail": "aaPanel site registration failed: tried [%s] — "
                      "none succeeded" % paths,
            "tried": tried}


def aapanel_remove_site(domain: str) -> bool:
    """Remove a site from aaPanel records (HTTP API first, then class API).

    Only a site JavaHost created is ever deleted (see panel_api.site_ownership).
    Returns True when the site is gone: removed here, or not there to begin
    with. Returns False when it could not be removed, including when it is not
    ours or its ownership could not be checked.
    """
    domain = validate.domain(domain)
    state = panel_api.site_ownership(domain)
    if state == "absent":
        return True
    if state != "javahost":
        return False
    removed = False

    # Path 1: HTTP API (most reliable — uses aaPanel's REST layer)
    removed = _aapanel_http_remove_site(domain)

    # Path 2: modern aaPanel class API
    # DeleteSite needs site ID — we pass None since we can't look it up
    # through the class API. aaPanel often accepts just webname without ID.
    if not removed:
        try:
            import sys
            panel_class = AAPANEL_PANEL_CLASS
            if panel_class not in sys.path:
                sys.path.insert(0, panel_class)
            from panelSite import panelSite as _SiteClass  # noqa: F401
            site_obj = _SiteClass()

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


def _owner_marker(app: str) -> str:
    from ..tomcat import instance
    app = validate.identifier(app, "app")
    return os.path.join(instance.base_path(app), "bin", "site.owner")


def _marker_dir(path: str) -> None:
    """The marker lives in <app>/bin, inside a tree the app user owns. Refuse to
    write (and chmod) through a `bin` that has been swapped for a link."""
    d = os.path.dirname(path)
    if os.path.islink(d):
        raise RuntimeError("refusing to write through a symlinked directory: %s" % d)
    os.makedirs(d, exist_ok=True)


def _store_domain(app: str, domain: str) -> None:
    path = _site_marker(app)
    _marker_dir(path)
    fs.atomic_write(path, domain + "\n", mode=0o644)


def _store_owner(app: str, owner: str) -> None:
    """Record who owns the public site config: 'aapanel' or 'javahost'."""
    owner = str(owner or "").strip().lower() or "aapanel"
    path = _owner_marker(app)
    _marker_dir(path)
    fs.atomic_write(path, owner + "\n", mode=0o644)


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
    try:
        op = _owner_marker(app)
        if os.path.exists(op):
            os.unlink(op)
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
            # The marker sits in a dir the app user can write, and its value is
            # used to build URLs, cert paths and aaPanel API calls.
            return validate.domain(f.read(512))
    except (OSError, ValueError):
        return None


def read_owner(app: str) -> str:
    """Who owns the public site nginx/ssl config: 'aapanel' (default) or 'javahost'."""
    try:
        path = _owner_marker(app)
    except Exception:
        return "aapanel"
    if not os.path.isfile(path):
        # Domains created via SetSite are aaPanel-owned; missing marker => aapanel.
        return "aapanel" if read_domain(app) else "javahost"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            v = f.read().strip().lower()
        return v if v in ("aapanel", "javahost") else "aapanel"
    except OSError:
        return "aapanel"


def set_site(app: str, domain: str, port: int) -> Dict:
    """Publish <app> at <domain> -> http://127.0.0.1:<port>.

    publish_mode "vhost" (default): a JavaHost-owned vhost file, no API key.
    publish_mode "panel": a panel website through aaPanel's APIs; on failure it
    returns an error and writes no nginx config of its own.
    Fail-closed when the active webserver is not nginx.
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)

    nginx_err = panel_api.require_nginx()
    if nginx_err:
        return {"ok": False, "error": nginx_err}

    # Ensure $connection_upgrade exists before aaPanel/proxy snippets reference it.
    # Writes to panel nginx.conf only via compat/aapanel (panel-coupling boundary).
    try:
        ws_map_ready = panel_api.ensure_ws_map()
    except Exception as exc:
        return {
            "ok": False,
            "error": ("WebSocket proxy prerequisite failed: could not install "
                      "or validate the nginx upgrade map: %s" % exc),
        }
    if not ws_map_ready:
        return {
            "ok": False,
            "error": ("WebSocket proxy prerequisite failed: aaPanel nginx.conf "
                      "is unavailable or nginx rejected the upgrade map."),
        }

    if config.publish_mode() != "panel":
        return _set_site_vhost(app, domain, port)

    # Changing domain: attach the NEW site first; only then drop the previous
    # aaPanel site. Delete-before-create orphans the live site when add fails.
    prev = read_domain(app)
    aap = aapanel_add_site(domain, port)
    if not aap.get("ok"):
        tried_str = ", ".join(aap.get("tried", [])) or aap.get("path", "aapanel")
        detail = aap.get("error") or aap.get("detail", "unknown error")
        hint = ""
        if "http-api-skipped-no-key" in aap.get("tried", []):
            hint = (" Configure aapanel_api_key in Settings to enable the "
                    "HTTP API fallback.")
        msg = ("aaPanel site registration failed [%s]: %s.%s "
               "Site not created — fix the issue and try again."
               % (tried_str, detail, hint))
        return {"ok": False, "error": msg}

    if prev and prev != domain:
        try:
            aapanel_remove_site(prev)
        except Exception:
            pass
        try:
            remove_vhost(app)
        except Exception:
            pass

    # Do not inject a competing JavaHost vhost — aaPanel owns the site conf.
    _store_domain(app, domain)
    _store_owner(app, "aapanel")
    # Remove any leftover plugin vhost for this app to avoid duplicate server_name.
    try:
        path = vhost_path(app)
        had = os.path.exists(path)
        remove_vhost(app)
        if had:
            reload_nginx()
    except Exception:
        pass
    return {"ok": True, "domain": domain, "url": "http://%s/" % domain,
            "via": "aapanel", "owner": "aapanel", "aapanel": aap.get("detail", "")}


_SERVER_NAME = re.compile(r"(?:^|(?<=[;{}]))\s*server_name\s+([^;]+);", re.M)


def _serves(name: str, domain: str) -> bool:
    """Does an nginx server_name entry cover <domain>? Regex names are not
    evaluated."""
    name = name.lower().rstrip(".")
    if name.startswith("*."):
        return domain.endswith(name[1:])
    if name.startswith("."):
        return domain == name[1:] or domain.endswith(name)
    return name == domain


def _conf_names(path: str) -> list:
    """Every server_name listed in an nginx file ([] when unreadable)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read(1 << 20)
    except OSError:
        return []
    return [n for m in _SERVER_NAME.finditer(body) for n in m.group(1).split()]


def vhost_domain(app: str) -> Optional[str]:
    """The domain in the app's vhost file. That file is root-owned, unlike the
    site.domain marker, which sits in a directory the app's user can write."""
    for p in (vhost_path(app), legacy_vhost_path(app)):
        names = _conf_names(p)
        if names:
            return names[0].lower()
    return None


def _domain_in_use(app: str, domain: str) -> Optional[str]:
    """Why <domain> cannot get a JavaHost vhost for <app>, or None.

    Reads every site file nginx loads from the panel's vhost folder, so alias
    and wildcard names of a panel website count, not just its file name."""
    domain = domain.lower()
    own = (vhost_path(app), legacy_vhost_path(app))
    for vdir, ours in ((VHOST_DIR, VHOST_PREFIX), (LEGACY_VHOST_DIR, "")):
        try:
            names = sorted(os.listdir(vdir))
        except OSError:
            continue
        for f in names:
            p = os.path.join(vdir, f)
            if p in own or not f.endswith(".conf"):
                continue
            if not any(_serves(n, domain) for n in _conf_names(p)):
                continue
            if f.startswith(ours) and (ours or vdir == LEGACY_VHOST_DIR):
                return "%s is already published by app %r" % (
                    domain, f[len(ours):-len(".conf")])
            return ("%s is already a website in the panel (%s). Remove it under "
                    "Website first, or set Settings > Publish domains to "
                    "'aaPanel website'." % (domain, f))
    return None


def _set_site_vhost(app: str, domain: str, port: int) -> Dict:
    prev, prev_owner = read_domain(app), read_owner(app)
    res = {"ok": True, "domain": domain, "url": "http://%s/" % domain,
           "via": "vhost", "owner": "javahost"}
    if (prev == domain and prev_owner == "aapanel"
            and os.path.exists(os.path.join(VHOST_DIR, "%s.conf" % domain))):
        # already live as this app's panel website; nothing to move
        return dict(res, via="aapanel", owner="aapanel")
    if vhost_domain(app) == domain.lower():
        for p in (vhost_path(app), legacy_vhost_path(app)):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    live = ("proxy_pass http://127.0.0.1:%d;" % port) in f.read()
            except OSError:
                continue
            if live:
                # Same domain, same port: leave the file (and HTTPS) as it is.
                _store_domain(app, domain)
                _store_owner(app, "javahost")
                return res
    from . import ssl as sslmod
    had_ssl = bool(sslmod.read_ssl(app))
    try:
        err = publish_vhost(app, domain, port)
    except OSError as e:
        err = "could not write %s: %s" % (vhost_path(app), e)
    if err:
        return {"ok": False, "error": "Could not publish %s: %s." % (domain, err.rstrip("."))}
    try:
        _store_domain(app, domain)
        _store_owner(app, "javahost")
        sslmod._mark_ssl(app, False)     # the file just written is plain HTTP
    except Exception as e:
        # a site JavaHost has no record of must not stay live
        remove_vhost(app)
        reload_nginx()
        return {"ok": False, "error": "Could not publish %s: %s." % (domain, e)}
    notes = []
    if had_ssl:
        notes.append("HTTPS was switched off by this change. Turn it on again "
                     "for %s." % domain)
    if prev and prev != domain and prev_owner == "aapanel":
        # The old name comes from a file the app's own user can write. Delete
        # the panel website only when the panel's own files say it is a
        # JavaHost site that proxies to this app's port.
        gone = False
        try:
            gone = (panel_api.site_proxies_to(prev, port)
                    and bool(aapanel_remove_site(prev)))
        except Exception:
            gone = False
        if not gone:
            notes.append("The previous panel website %s was left in place. "
                         "Remove it under Website if it is no longer needed." % prev)
    if notes:
        res["note"] = " ".join(notes)
    return res


def remove_site(app: str) -> Dict:
    """Remove the app's vhost + aaPanel site record.

    Clears the domain marker only when aaPanel cleanup succeeded (or there was
    no domain). On aaPanel failure, keeps the marker so the operator can retry
    RemoveSite without losing the domain name.
    """
    app = validate.identifier(app, "app")
    domain = read_domain(app)

    aapanel_removed = False
    aapanel_error = None
    left_in_place = None
    # published as a JavaHost vhost: there is no panel website to remove
    vhost_only = bool(domain) and read_owner(app) == "javahost"
    if vhost_only:
        aapanel_removed = True
    elif domain and panel_api.site_ownership(domain) == "foreign":
        # The site was created by the operator (JavaHost only attached a proxy to
        # it), so it is not ours to delete, and it must not block removing the app.
        left_in_place = domain
        domain = None
    if domain and not vhost_only:
        try:
            aapanel_removed = bool(aapanel_remove_site(domain))
        except Exception as e:
            aapanel_error = str(e)
        if not aapanel_removed and aapanel_error is None:
            aapanel_error = "aaPanel site delete did not confirm removal"

    # Always drop a plugin-owned vhost if present (idempotent).
    try:
        remove_vhost(app)
        reload_nginx()
    except Exception:
        pass

    if domain and not aapanel_removed:
        # Keep domain/owner markers for retry; surface partial failure.
        return {
            "app": app,
            "removed": False,
            "aapanel_cleaned": False,
            "domain": domain,
            "error": aapanel_error or "aaPanel cleanup failed — domain marker kept for retry",
        }

    _clear_domain(app)
    # Also clear SSL marker so we don't report ssl=true with no domain.
    try:
        from . import ssl as sslmod
        sslmod._mark_ssl(app, False)
    except Exception:
        pass
    out = {"app": app, "removed": True, "aapanel_cleaned": aapanel_removed,
           "domain": domain or left_in_place}
    if left_in_place:
        out["note"] = ("aaPanel site %s was not created by JavaHost and was left in "
                       "place; remove it or its reverse proxy under Website if it is "
                       "no longer needed" % left_in_place)
    return out
