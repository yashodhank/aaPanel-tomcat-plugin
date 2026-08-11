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

import os
import re
import time
from typing import Dict, Optional, Tuple

from . import proxy
from ..compat import aapanel as panel_api
from ..util import fs, shell, validate

# Per-instance SSL state marker (lives next to site.domain, written by proxy).
SSL_MARKER_NAME = "site.ssl"
# certbot deploy hook: runs after every successful renewal.
RENEWAL_HOOK = "/etc/letsencrypt/renewal-hooks/deploy/javahost-nginx.sh"
_RENEWAL_HOOK_BODY = """#!/bin/sh
# Managed by JavaHost. Reload nginx after LE renewal and refresh SSL markers.
nginx -s reload
python3 - <<'PY'
try:
    from core.deploy import ssl as jh_ssl
    jh_ssl.refresh_all_markers()
except Exception:
    pass
PY
"""

# aaPanel panel-managed certificate directory (common on Nginx installs).
AAPANEL_CERT_ROOT = "/www/server/panel/vhost/cert"


def _live_fullchain(domain: str) -> Optional[str]:
    """Return path to a usable fullchain for <domain>, or None.

    Checks Let's Encrypt live dir first, then aaPanel's panel cert store.
    """
    candidates = [
        "/etc/letsencrypt/live/%s/fullchain.pem" % domain,
        os.path.join(AAPANEL_CERT_ROOT, domain, "fullchain.pem"),
        os.path.join(AAPANEL_CERT_ROOT, domain, "cert.pem"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _cert_exists(domain: str) -> bool:
    """Whether a live cert is present for <domain>. Isolated so tests can
    monkeypatch it instead of touching /etc."""
    return _live_fullchain(domain) is not None


def _cert_not_after(domain: str) -> Optional[str]:
    """notAfter (ISO 8601) of the live fullchain, via `openssl x509 -enddate`.
    Best-effort: returns None if the cert/openssl is absent or unparseable.
    Never raises (the cert was just placed; this is only for the cheap marker)."""
    path = _live_fullchain(domain)
    if not path:
        return None
    try:
        # openssl: fixed argv, no shell; path is the plugin-known live cert file.
        rc, out, _ = shell.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", path],
            check=False, timeout=10)
        if rc != 0 or not out:
            return None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("notAfter="):
                raw = line.split("=", 1)[1].strip()
                t = time.strptime(raw, "%b %d %H:%M:%S %Y %Z")
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)
    except Exception:
        return None
    return None


def _ssl_marker(app: str) -> str:
    """<INSTANCE_ROOT>/<app>/bin/site.ssl. Lazy import to avoid an import cycle."""
    from ..tomcat import instance
    app = validate.identifier(app, "app")
    return os.path.join(instance.base_path(app), "bin", SSL_MARKER_NAME)


def _mark_ssl(app: str, on: bool, not_after: Optional[str] = None) -> None:
    """Write/clear the per-app SSL marker. When enabling, store the cert's
    not_after (ISO 8601) as the marker contents so the list view can show expiry
    cheaply without an openssl/network probe; falls back to "1" when unknown.
    read_ssl() only cares that the file EXISTS, so any non-empty body is truthy."""
    path = _ssl_marker(app)
    if on:
        fs.ensure_dir(os.path.dirname(path))
        fs.atomic_write(path, (not_after or "1") + "\n", mode=0o644)
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


def read_ssl_not_after(app: str) -> Optional[str]:
    """The cert's notAfter (ISO 8601) recorded in the SSL marker body when SSL
    was enabled, or None when SSL is off / the marker is the legacy "1" sentinel
    (cert present but expiry unknown). Cheap: a single small file read, NO openssl
    or network call — this is what lets the dashboard flag expiring certs on its
    hot path. Defensive: never raises."""
    try:
        path = _ssl_marker(app)
        if not os.path.isfile(path):
            return None
        body = open(path, errors="replace").read().strip()
    except Exception:
        return None
    # "1" (or empty) = enabled-but-expiry-unknown; only an ISO date is useful here.
    if not body or body == "1":
        return None
    return body


# --- issuance backends -------------------------------------------------------
def _aapanel_apply(domain: str) -> Optional[bool]:
    """Issue via aaPanel's native ACME HTTP API on the loopback panel.

    Returns True on a parsed success, False on a parsed/transport failure, and
    None when no api_sk is configured (caller then moves on to certbot).
    Delegates to compat.aapanel (sole panel HTTP coupling).
    """
    return panel_api.http_apply_cert(domain)


def _certbot_issue(domain: str, email: Optional[str] = None):
    """Issue via `certbot certonly --webroot`.

    Returns (ok, error) where ok = rc==0 AND a live cert now exists, and error is
    the tail of certbot's combined stdout+stderr on failure (None on success).
    Certbot's output carries no secrets, so surfacing it helps operators see the
    real reason (rate-limit, DNS, challenge unreachable). Never raises."""
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
        rc, out, err = shell.run(argv, check=False, timeout=300)
        if rc == 0 and _cert_exists(domain):
            return True, None
        detail = ((out or "") + (err or "")).strip()
        detail = detail[-500:] if detail else "certbot rc=%s (no output)" % rc
        return False, detail
    except Exception as e:
        return False, "certbot invocation failed: %s" % e


def _install_renewal_hook() -> None:
    """Idempotently install the certbot deploy hook that reloads nginx after a
    renewal. Best-effort: never raises."""
    try:
        fs.ensure_dir(os.path.dirname(RENEWAL_HOOK))
        fs.atomic_write(RENEWAL_HOOK, _RENEWAL_HOOK_BODY, mode=0o755)
    except Exception:
        pass


# --- public API --------------------------------------------------------------
def _find_wildcard_cert(domain: str) -> Tuple[Optional[str], Optional[str]]:
    """Check if a single-label wildcard cert covers *domain*.

    ``*.example.com`` matches ``app.example.com`` only — never multi-label hosts
    like ``a.b.example.com``. Returns (base_domain, wildcard_name) or (None, None).
    """
    parts = domain.split(".")
    if len(parts) < 2:
        return None, None
    # Only the immediate parent may carry a covering wildcard (DNS semantics).
    base = ".".join(parts[1:])
    cert_dir = os.path.join("/etc/letsencrypt/live", base)
    pem = os.path.join(cert_dir, "fullchain.pem")
    if not os.path.isfile(pem):
        return None, None
    try:
        rc, out, _ = shell.run(
            ["openssl", "x509", "-text", "-noout", "-in", pem],
            check=False, timeout=10)
        if rc != 0 or not out:
            return None, None
        wildcard_pattern = r"DNS:\*\." + re.escape(base)
        if re.search(wildcard_pattern, out):
            return base, "*." + base
    except Exception:
        return None, None
    return None, None


def refresh_all_markers() -> int:
    """Re-read not_after for every app with ssl enabled; used by renew hook."""
    from ..tomcat import instance
    n = 0
    try:
        apps = instance.list_apps()
    except Exception:
        return 0
    for info in apps or []:
        app = (info or {}).get("app")
        if not app or not read_ssl(app):
            continue
        domain = proxy.read_domain(app)
        if not domain:
            continue
        # Prefer wildcard base cert if that is what covers this host.
        cert_domain, _ = _find_wildcard_cert(domain)
        dig = cert_domain if cert_domain and _cert_exists(cert_domain) else domain
        if not _cert_exists(dig):
            continue
        _mark_ssl(app, True, not_after=_cert_not_after(dig))
        n += 1
    return n


def _activate_plugin_vhost(app: str, domain: str, port: int, *,
                           ssl_on: bool, cert_domain: Optional[str] = None,
                           wildcard_name: Optional[str] = None) -> Optional[str]:
    """Write plugin vhost and reload nginx fail-closed. Returns error or None."""
    proxy.write_vhost(app, domain, port, ssl=ssl_on,
                      cert_domain=cert_domain, wildcard_name=wildcard_name)
    if not proxy.ensure_include():
        return "failed to ensure JavaHost nginx include (nginx -t rejected?)"
    if not proxy.reload_nginx():
        return "nginx -t / reload failed after writing SSL vhost"
    return None


def enable(app: str, domain: str, port: int, email: Optional[str] = None) -> Dict:
    """Provision SSL for <app> at <domain> -> 127.0.0.1:<port>.

    When the site is aaPanel-owned (SetSite path), SSL is enabled through aaPanel
    APIs so we do not write a competing JavaHost nginx vhost. Plugin vhosts are
    used only for javahost-owned / certbot fallback sites, fail-closed on reload.
    """
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    owner = proxy.read_owner(app)

    # --- aaPanel-owned site: let the panel manage the vhost ------------------
    if owner == "aapanel" and panel_api.api_key_configured():
        # Prefer reusing an existing covering wildcard without rewriting nginx.
        cert_domain, wildcard_name = _find_wildcard_cert(domain)
        if cert_domain and _cert_exists(cert_domain):
            not_after = _cert_not_after(cert_domain)
            _mark_ssl(app, True, not_after=not_after)
            return {"ssl": True, "url": "https://%s/" % domain,
                    "via": "wildcard", "owner": "aapanel",
                    "cert_domain": cert_domain, "wildcard": wildcard_name}
        if panel_api.http_enable_site_ssl(domain) and _cert_exists(domain):
            not_after = _cert_not_after(domain)
            _mark_ssl(app, True, not_after=not_after)
            return {"ssl": True, "url": "https://%s/" % domain,
                    "via": "aapanel", "owner": "aapanel",
                    "not_after": not_after} if not_after else {
                        "ssl": True, "url": "https://%s/" % domain,
                        "via": "aapanel", "owner": "aapanel"}
        # Native path did not place a discoverable cert — fall through to
        # certbot against the *existing* aaPanel HTTP site (no plugin vhost).

    # --- plugin / certbot path (javahost-owned or aaPanel ACME miss) ---------
    fs.ensure_dir(proxy.ACME_WEBROOT)

    # Only write a competing HTTP vhost when the site is javahost-owned.
    if owner != "aapanel":
        err = _activate_plugin_vhost(app, domain, port, ssl_on=False)
        if err:
            return {"ssl": False, "error": err}

    cert_domain, wildcard_name = _find_wildcard_cert(domain)
    if cert_domain and _cert_exists(cert_domain):
        not_after = _cert_not_after(cert_domain)
        if owner != "aapanel":
            err = _activate_plugin_vhost(
                app, domain, port, ssl_on=True,
                cert_domain=cert_domain, wildcard_name=wildcard_name)
            if err:
                return {"ssl": False, "error": err}
            proxy._store_owner(app, "javahost") if hasattr(proxy, "_store_owner") else None
        _install_renewal_hook()
        _mark_ssl(app, True, not_after=not_after)
        return {"ssl": True, "url": "https://%s/" % domain,
                "via": "wildcard", "cert_domain": cert_domain,
                "wildcard": wildcard_name}

    via = None
    certbot_err = None
    if owner != "aapanel":
        if _aapanel_apply(domain) and _cert_exists(domain):
            via = "aapanel"
    if not via:
        # certbot is optional — surface a clear error when the binary is missing
        if not shell.which("certbot"):
            certbot_err = ("certbot not installed (optional dependency for "
                           "SSL when aaPanel ACME is unavailable)")
        else:
            ok, certbot_err = _certbot_issue(domain, email)
            if ok:
                via = "certbot"

    if _cert_exists(domain):
        not_after = _cert_not_after(domain)
        if owner != "aapanel":
            err = _activate_plugin_vhost(app, domain, port, ssl_on=True)
            if err:
                return {"ssl": False, "error": err}
            try:
                proxy._store_owner(app, "javahost")
            except Exception:
                pass
        elif via == "certbot":
            # Cert issued into LE live dir but aaPanel site still owns HTTP —
            # do NOT write a duplicate vhost; ask panel to pick up LE paths if possible.
            panel_api.http_enable_site_ssl(domain)
        _install_renewal_hook()
        _mark_ssl(app, True, not_after=not_after)
        res = {"ssl": True, "url": "https://%s/" % domain,
               "via": via or "unknown", "owner": owner}
        if not_after:
            res["not_after"] = not_after
        return res

    error = "certificate issuance failed (native + certbot)"
    if certbot_err:
        error += ": " + certbot_err
    return {"ssl": False, "error": error}


def disable(app: str, domain: str, port: int) -> Dict:
    """Revert <app> to plain HTTP. Cert is KEPT on disk so re-enable is instant."""
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    owner = proxy.read_owner(app)
    if owner == "aapanel":
        panel_api.http_disable_site_ssl(domain)
        _mark_ssl(app, False)
        return {"ssl": False, "url": "http://%s/" % domain, "owner": "aapanel"}
    err = _activate_plugin_vhost(app, domain, port, ssl_on=False)
    if err:
        # Still clear the marker only if reload succeeded historically; fail closed.
        return {"ssl": True, "error": err, "url": "https://%s/" % domain}
    _mark_ssl(app, False)
    return {"ssl": False, "url": "http://%s/" % domain}
