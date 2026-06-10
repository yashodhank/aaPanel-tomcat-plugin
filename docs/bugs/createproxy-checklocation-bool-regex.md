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

The `CheckLocation()` call at line 4064 is **outside** the `if not nocheck:` guard block. It runs unconditionally when the webserver is nginx (which it always is on this VPS).

`CheckLocation()` calls `re.findall(rep, conf)` where `conf` comes from `self.__read_config()` — which returns a **bool** instead of a string in some code paths.

**Result:** `TypeError: expected string or bytes-like object, got 'bool'` — crashes even with `nocheck="1"` set.

## Impact

- Cannot create reverse-proxy sites via aaPanel's Python API (`panelSite.CreateProxy`)
- The HTTP API path (`POST /site?action=AddSite`) works correctly when `api_sk` is configured
- Plugin falls back to nginx vhost as last resort

## Workaround (implemented in JavaHost v0.28.1)

1. `_try_aapanel_class_api()` catches the TypeError and returns None
2. Falls through to `_try_aapanel_http_api()` which calls aaPanel's HTTP API with `api_sk`
3. If HTTP API also fails, falls back to plugin-owned nginx vhost with warning

## To fix in aaPanel

Move `CheckLocation` inside the `if not nocheck:` guard block (same as `__CheckStart`).
Or add `if not nocheck:` before line 4064:

```python
if not nocheck:
    if public.get_webserver() == 'nginx':
        if self.CheckLocation(get):
            return self.CheckLocation(get)
```
