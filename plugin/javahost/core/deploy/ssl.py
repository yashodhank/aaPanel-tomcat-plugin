# coding: utf-8
"""
Per-site SSL provisioning for JavaHost reverse-proxy sites.

Strategy (enable):
  1. NATIVE first — aaPanel's own ACME via its local HTTP API
     (`/acme?action=apply_cert_api`). Only attempted when an interface API key
     (`api_sk`) is configured for the plugin; otherwise SKIPPED. On some hosts
     aaPanel's bundled `sewer` ACME client is broken against pyOpenSSL >= 24
     ("Invalid version. X509Req") — which is exactly WHY the certbot fallback
     exists below.
  2. FALLBACK — certbot webroot (HTTP-01), serving challenges from the same
     ACME_WEBROOT the vhost already exposes.

The HTTP vhost is written/reloaded BEFORE issuance so the challenge URL is
reachable. Once a live cert exists, the vhost is rewritten ssl=True (adds the 443
server + 301 redirect) and an idempotent certbot deploy-hook is installed so
renewals reload nginx automatically. State is recorded in a per-instance marker
(<base>/bin/site.ssl) read by list_apps().

Stdlib only. Every network/subprocess interaction is wrapped so callers get a
structured result, never an exception, and unit tests can mock the boundaries
(`_aapanel_apply`, `_certbot_issue`, `proxy.reload_nginx`, the live-path check).
"""
from __future__ import annotations

import json
import os
import ssl as _sslmod
import urllib.parse
import urllib.request
from typing import Dict, Optional

from . import proxy
from .. import config
from ..util import fs, validate

# Per-instance SSL state marker (lives next to site.domain, written by proxy).
SSL_MARKER_NAME = "site.ssl"
# certbot deploy hook: runs after every successful renewal.
RENEWAL_HOOK = "/etc/letsencrypt/renewal-hooks/deploy/javahost-nginx.sh"
_RENEWAL_HOOK_BODY = "#!/bin/sh\n# Managed by JavaHost. Reload nginx after LE renewal.\nnginx -s reload\n"


def _live_fullchain(domain: str) -> str:
    return "/etc/letsencrypt/live/%s/fullchain.pem" % domain


def _cert_exists(domain: str) -> bool:
    """Whether a live LE cert is present for <domain>. Isolated so tests can
    monkeypatch it instead of touching /etc."""
    return os.path.isfile(_live_fullchain(domain))


def _ssl_marker(app: str) -> str:
    """<INSTANCE_ROOT>/<app>/bin/site.ssl. Lazy import to avoid an import cycle."""
    from ..tomcat import instance
    app = validate.identifier(app, "app")
    return os.path.join(instance.base_path(app), "bin", SSL_MARKER_NAME)


def _mark_ssl(app: str, on: bool) -> None:
    path = _ssl_marker(app)
    if on:
        fs.ensure_dir(os.path.dirname(path))
        fs.atomic_write(path, "1", mode=0o644)
    elif os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def read_ssl(app: str) -> bool:
    """True if the SSL marker is present for <app>. Defensive: never raises."""
    try:
        path = _ssl_marker(app)
    except Exception:
        return False
    try:
        return os.path.isfile(path)
    except Exception:
        return False


# --- issuance backends -------------------------------------------------------
def _aapanel_apply(domain: str) -> Optional[bool]:
    """Issue via aaPanel's native ACME HTTP API on the loopback panel.

    Returns True on a parsed success, False on a parsed/transport failure, and
    None when no api_sk is configured (caller then moves on to certbot). Auth
    follows the documented scheme:
        request_time  = int(time())
        request_token = md5(str(request_time) + md5(api_sk))
    POSTed as form fields alongside the action params to
    https://127.0.0.1:<port>/acme?action=apply_cert_api (verify disabled —
    loopback, self-signed panel cert).
    """
    api_sk = config.aapanel_api_key()
    if not api_sk:
        return None
    try:
        import hashlib
        import time

        port = config.aapanel_port()
        request_time = int(time.time())
        # MD5 is mandated by aaPanel's API token scheme (request_token =
        # md5(request_time + md5(api_sk))) — not a security primitive of ours.
        sk_md5 = hashlib.md5(api_sk.encode()).hexdigest()  # nosec B324
        token = hashlib.md5(
            (str(request_time) + sk_md5).encode()
        ).hexdigest()  # nosec B324
        params = {
            "request_time": str(request_time),
            "request_token": token,
            "domains": json.dumps([domain]),
            "siteName": domain,
            "auth_type": "http",
            "auth_to": domain,
        }
        body = urllib.parse.urlencode(params).encode()
        url = "https://127.0.0.1:%d/acme?action=apply_cert_api" % port
        ctx = _sslmod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _sslmod.CERT_NONE
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:  # noqa: S310 (loopback)
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            return bool(data.get("status") or data.get("success") or data.get("cert"))
        return False
    except Exception:
        return False


def _certbot_issue(domain: str, email: Optional[str] = None) -> bool:
    """Issue via `certbot certonly --webroot`. Success = rc==0 AND a live cert
    now exists. Never raises."""
    from ..util import shell
    try:
        argv = [
            "certbot", "certonly", "--webroot",
            "-w", proxy.ACME_WEBROOT,
            "-d", domain,
            "--non-interactive", "--agree-tos", "--keep-until-expiring",
        ]
        if email:
            argv += ["-m", email]
        else:
            argv += ["--register-unsafely-without-email"]
        rc, _, _ = shell.run(argv, check=False, timeout=300)
        return rc == 0 and _cert_exists(domain)
    except Exception:
        return False


def _install_renewal_hook() -> None:
    """Idempotently install the certbot deploy hook that reloads nginx after a
    renewal. Best-effort: never raises."""
    try:
        fs.ensure_dir(os.path.dirname(RENEWAL_HOOK))
        fs.atomic_write(RENEWAL_HOOK, _RENEWAL_HOOK_BODY, mode=0o755)
    except Exception:
        pass


# --- public API --------------------------------------------------------------
def enable(app: str, domain: str, port: int, email: Optional[str] = None) -> Dict:
    """Provision SSL for <app> at <domain> -> 127.0.0.1:<port>.

    Ensures the ACME webroot + an HTTP vhost (so the challenge is reachable),
    then issues a cert: aaPanel native first, certbot fallback. On success the
    vhost is rewritten ssl=True, nginx reloaded, the renewal hook installed, and
    the SSL marker set. Returns {"ssl":True,"url":...,"via":...} or, on issuance
    failure, {"ssl":False,"error":...} (HTTP vhost left intact).
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)

    # 1) make the challenge reachable: webroot + HTTP vhost + reload
    fs.ensure_dir(proxy.ACME_WEBROOT)
    proxy.write_vhost(app, domain, port, ssl=False)
    proxy.ensure_include()
    proxy.reload_nginx()

    # 2) issue — native first, certbot fallback
    via = None
    if _aapanel_apply(domain):
        via = "aapanel"
    elif _certbot_issue(domain, email):
        via = "certbot"

    if via and _cert_exists(domain):
        proxy.write_vhost(app, domain, port, ssl=True)
        proxy.reload_nginx()
        _install_renewal_hook()
        _mark_ssl(app, True)
        return {"ssl": True, "url": "https://%s/" % domain, "via": via}

    return {"ssl": False, "error": "certificate issuance failed (native + certbot)"}


def disable(app: str, domain: str, port: int) -> Dict:
    """Revert <app> to plain HTTP: rewrite vhost ssl=False, reload, clear the SSL
    marker. The cert is KEPT on disk so re-enable is instant."""
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    proxy.write_vhost(app, domain, port, ssl=False)
    proxy.reload_nginx()
    _mark_ssl(app, False)
    return {"ssl": False, "url": "http://%s/" % domain}
