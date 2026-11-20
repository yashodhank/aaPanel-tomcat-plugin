# BUG: aaPanel's CreateProxy crashes on CheckLocation bool/regex mismatch

**Date:** 2026-06-10
**Severity:** High (blocks reverse-proxy site creation via panelSite Python API)
**Status:** Open — aaPanel internal bug, not fixable from plugin side
**aaPanel version:** Observed on aaPanel 7.x

## Root cause

Observed from the outside, by calling the panel's site class from the plugin
(no panel source is reproduced here, per the project's clean-room rule):

- `CreateProxy` accepts a `nocheck` parameter that skips its start-up checks.
- On nginx it still runs a location check on the site's config, whatever
  `nocheck` says.
- That check runs a regular expression over the site config. In some cases the
  config read hands back a boolean instead of text.

**Result:** `TypeError: expected string or bytes-like object, got 'bool'`, raised
even with `nocheck="1"`.

## Impact

- Cannot create reverse-proxy sites via aaPanel's Python class API (`panelSite.CreateProxy`)
- The HTTP API path (`POST /site?action=AddSite` + `CreateProxy`) works when `api_sk` / Settings **API key** is configured
- JavaHost does **not** fall back to a plugin-owned nginx vhost for SetSite (aaPanel owns the site)

## Workaround (JavaHost ≥ 0.28 / current)

1. Configure **Settings → aaPanel API key** (`aapanel_api_key`) — required on aaPanel 7.x
2. `_try_aapanel_class_api()` catches the TypeError and returns `None`
3. HTTP path runs first when the key is set: `AddSite` + `CreateProxy` (with retries)
4. If HTTP `CreateProxy` still fails (wrong nginx context), JavaHost writes aaPanel's own
   `vhost/nginx/proxy/<domain>/*.conf` + `proxyfile.json` include layout (not a competing
   JavaHost vhost), then `nginx -t` + reload
5. If both CreateProxy and the file fallback fail after AddSite created a shell site,
   JavaHost never calls `DeleteSite` automatically: aaPanel offers no conditional
   ownership precondition at deletion time. It returns `panel.err`, `site_may_remain`,
   the AddSite ID when available, and explicit manual verification/cleanup guidance
6. Non-nginx webservers (Apache / OpenLiteSpeed) fail closed with a clear error — no
   silent “success” with dead traffic

Without an API key, SetSite tries class/legacy APIs only and typically fails on 7.x;
configure the key rather than expecting a plugin nginx fallback.

## To fix in aaPanel

The location check should be skipped when `nocheck` is set, as the start-up
checks are, and it should cope with a config read that did not return text.
