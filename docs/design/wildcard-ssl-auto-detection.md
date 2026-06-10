# Wildcard SSL — Design Analysis

**Date:** 2026-06-10
**Status:** Design proposal
**Blocked by:** Need wildcard cert on VPS (none present), or need DNS API credentials

## Current state

JavaHost's `SetSiteSSL` issues per-site Let's Encrypt certs via HTTP-01 challenge only.
Each site gets its own cert (e.g. `app1.5d.bisotech.in`). This works but creates:
- Rate limit pressure (LE allows 50 certs per domain per week)
- Many cert renewal hooks
- Slow per-site issuance (~30s each)

## Proposed: Auto-use existing wildcard cert

If a wildcard cert exists for `*.5d.bisotech.in` (at `/etc/letsencrypt/live/5d.bisotech.in/`),
the plugin should auto-detect it and skip per-site cert issuance.

### Discovery

```python
def _find_wildcard_cert(domain: str) -> Optional[str]:
    """Find a wildcard cert covering domain. Returns base domain path or None."""
    parts = domain.split(".")
    for i in range(1, len(parts)):
        base = ".".join(parts[i:])
        cert_dir = "/etc/letsencrypt/live/%s" % base
        pem = os.path.join(cert_dir, "fullchain.pem")
        if not os.path.isfile(pem):
            continue
        # Check SAN for *.base
        # openssl x509 -in pem -text -noout | grep 'DNS:\*\.%s' % base
        ...
```

### Modified enable() flow

```
SetSiteSSL(app, enable)
  ├─ Check for existing wildcard cert covering domain
  │   ├─ FOUND → use it (skip issuance), write HTTPS vhost
  │   └─ NOT FOUND → per-site issuance (aaPanel native → certbot)
  └─ Install renewal hook (not needed for wildcard)
```

### Template changes

Current:
```nginx
server_name @@domain@@;
ssl_certificate /etc/letsencrypt/live/@@domain@@/fullchain.pem;
```

Proposed:
```nginx
server_name @@domain@@ @@wildcard_name@@;
ssl_certificate /etc/letsencrypt/live/@@cert_domain@@/fullchain.pem;
```

Where `@@wildcard_name@@` = `*.base.domain` and `@@cert_domain@@` = base domain.

### DNS API setup for wildcard issuance

To issue a wildcard cert through aaPanel:
1. Configure DNS API in `config/dns_api.json` (e.g. Cloudflare API token)
2. Use `acme_v2.py` with `auto_wildcard = True` and `type = "dns"`
3. This requires panel-level DNS API setup, not plugin-level

## Recommendation

Phase 1: Auto-detect existing wildcard certs (no DNS API needed)
Phase 2: Support wildcard issuance via aaPanel DNS-01 (requires DNS API setup)
