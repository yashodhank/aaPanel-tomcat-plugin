# BUG: aaPanel's CreateProxy crashes on CheckLocation bool/regex mismatch

**Date:** 2026-06-10
**Severity:** High (blocks reverse-proxy site creation via panelSite Python API)
**Status:** Open — aaPanel internal bug, not fixable from plugin side
**aaPanel version:** Observed on aaPanel 7.x (panelSite.py, line 4064)

## Root cause

In `/www/server/panel/class/panelSite.py`, `CreateProxy()` (line ~4064):

```python
def CreateProxy(self, get):
    try:
        nocheck = get.nocheck
    except:
        nocheck = ""
    if not get.get('proxysite', None):
        return public.returnMsg(False, ...)
    if not nocheck:
        if self.__CheckStart(get, "create"):  # guarded by nocheck
            return self.__CheckStart(get, "create")
    if public.get_webserver() == 'nginx':
        if self.CheckLocation(get):           # NOT guarded — runs always on nginx!
            return self.CheckLocation(get)
```

The `CheckLocation()` call at line 4064 is **outside** the `if not nocheck:` guard block. It runs unconditionally when the webserver is nginx.

`CheckLocation()` calls `re.findall(rep, conf)` where `conf` comes from `self.__read_config()` — which returns a **bool** instead of a string in some code paths.

**Result:** `TypeError: expected string or bytes-like object, got 'bool'` — crashes even with `nocheck="1"` set.

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

Move `CheckLocation` inside the `if not nocheck:` guard block (same as `__CheckStart`).
Or add `if not nocheck:` before line 4064:

```python
if not nocheck:
    if public.get_webserver() == 'nginx':
        if self.CheckLocation(get):
            return self.CheckLocation(get)
```
