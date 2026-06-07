# coding: utf-8
"""
Remote object-storage *profiles* for backups (S3-compatible, multi-destination).

A registry of named profiles (remotes.json, 0600) — each a self-contained S3
destination (Wasabi / MinIO / Backblaze B2 / Cloudflare R2 / AWS). Backups can be
pushed to ONE OR MORE selected profiles; a backup's "locations" is the union of the
local store + whichever profiles hold it.

Secret keys live only in the 0600 registry and are NEVER returned by list/get
(only a `secret_set` flag), mirroring the GetDbEnv secret-safe pattern. All S3 work
is delegated to the dependency-free client in s3.py.

Back-compat: a legacy single-config remote.json is auto-migrated to one profile
id="default" on first load, and the old single-config helpers (configured/get_config/
set_config/remove/test/upload/download/delete/list_remote) are kept as thin wrappers.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

from ..util import fs, validate
from . import s3

REGISTRY_PATH = "/www/server/javahost/remotes.json"
LEGACY_PATH = "/www/server/javahost/remote.json"
PROVIDERS = ("wasabi", "minio", "backblaze", "r2", "aws", "other")
_REQUIRED = ("endpoint", "bucket", "access_key", "secret_key")
# Region-specific endpoint templates — for these providers the region MUST be in the
# endpoint or requests fail (e.g. a us-east-1 endpoint can't reach an ap-southeast-1
# bucket). Used to derive the endpoint when an API caller omits it.
_ENDPOINT_TPL = {
    "wasabi": "https://s3.{region}.wasabisys.com",
    "aws": "https://s3.{region}.amazonaws.com",
    "backblaze": "https://s3.{region}.backblazeb2.com",
}


def canonical_endpoint(provider: str, region: str) -> str:
    """Region-specific endpoint for a known provider, or '' if it can't be derived."""
    tpl = _ENDPOINT_TPL.get(provider or "")
    return tpl.replace("{region}", region) if (tpl and region) else ""
_BACKUP_RE = re.compile(r"^backup-(?P<app>[A-Za-z0-9._-]+)-\d{8}T\d{6}Z\.tar\.gz$")


# --------------------------------------------------------------------------- #
# registry I/O (+ legacy migration)
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip().lower()).strip("-._")
    return s or "profile"


def _load() -> Dict:
    """Load the registry, migrating a legacy single-config remote.json once."""
    try:
        with open(REGISTRY_PATH) as f:
            reg = json.load(f)
        if isinstance(reg, dict) and isinstance(reg.get("profiles"), list):
            return reg
    except Exception:
        pass
    # migration: legacy remote.json -> one "default" profile
    try:
        if os.path.isfile(LEGACY_PATH):
            with open(LEGACY_PATH) as f:
                c = json.load(f)
            if all(c.get(k) for k in _REQUIRED):
                prof = {
                    "id": "default", "name": c.get("provider", "default") or "default",
                    "provider": c.get("provider", "other"), "endpoint": c["endpoint"],
                    "region": c.get("region", "us-east-1"), "bucket": c["bucket"],
                    "access_key": c["access_key"], "secret_key": c["secret_key"],
                    "prefix": c.get("prefix", ""), "path_style": bool(c.get("path_style", True)),
                    "enabled": True, "created_at": _now_iso(),
                }
                reg = {"profiles": [prof]}
                _save(reg)
                return reg
    except Exception:
        pass
    return {"profiles": []}


def _save(reg: Dict) -> None:
    fs.ensure_dir(os.path.dirname(REGISTRY_PATH))
    fs.atomic_write(REGISTRY_PATH, json.dumps(reg, indent=2) + "\n", mode=0o600)


def _find(reg: Dict, pid: str) -> Optional[Dict]:
    for p in reg.get("profiles", []):
        if p.get("id") == pid:
            return p
    return None


def _redact(p: Dict) -> Dict:
    return {
        "id": p.get("id"), "name": p.get("name", p.get("id")),
        "provider": p.get("provider", "other"), "endpoint": p.get("endpoint", ""),
        "region": p.get("region", ""), "bucket": p.get("bucket", ""),
        "access_key": p.get("access_key", ""), "prefix": p.get("prefix", ""),
        "path_style": bool(p.get("path_style", True)),
        "enabled": bool(p.get("enabled", True)),
        "secret_set": bool(p.get("secret_key")),
        "configured": all(p.get(k) for k in _REQUIRED),
        "created_at": p.get("created_at"),
    }


# --------------------------------------------------------------------------- #
# profile CRUD
# --------------------------------------------------------------------------- #
def list_profiles(redacted: bool = True) -> List[Dict]:
    profs = _load().get("profiles", [])
    return [_redact(p) for p in profs] if redacted else list(profs)


def get_profile(pid: str, redacted: bool = True) -> Optional[Dict]:
    p = _find(_load(), pid)
    if not p:
        return None
    return _redact(p) if redacted else dict(p)


def configured() -> bool:
    """True if at least one usable profile exists."""
    return bool(enabled_ids())


def enabled_ids() -> List[str]:
    return [p["id"] for p in _load().get("profiles", [])
            if p.get("enabled", True) and all(p.get(k) for k in _REQUIRED)]


def add_profile(name: str, provider: str, endpoint: str, region: str, bucket: str,
                access_key: str, secret_key: str, prefix: str = "",
                path_style: bool = True, pid: str = "", enabled: bool = True) -> Dict:
    endpoint = (endpoint or "").strip()
    bucket = (bucket or "").strip()
    access_key = (access_key or "").strip()
    secret_key = secret_key or ""
    region = (region or "us-east-1").strip()
    # derive a region-specific endpoint when an API caller omits it for a known provider
    if not endpoint:
        endpoint = canonical_endpoint(provider, region)
    if not (endpoint and bucket and access_key and secret_key):
        raise ValueError("endpoint, bucket, access_key and secret_key are required "
                         "(region-based providers like wasabi/aws/backblaze also need a region)")
    pid = validate.identifier(pid or _slug(name or bucket), "profile id")
    reg = _load()
    if _find(reg, pid):
        raise ValueError("a storage profile with id %r already exists" % pid)
    if provider and provider not in PROVIDERS:
        provider = "other"
    reg.setdefault("profiles", []).append({
        "id": pid, "name": (name or pid).strip(), "provider": provider or "other",
        "endpoint": endpoint, "region": (region or "us-east-1").strip(), "bucket": bucket,
        "access_key": access_key, "secret_key": secret_key,
        "prefix": (prefix or "").strip().strip("/"), "path_style": bool(path_style),
        "enabled": bool(enabled), "created_at": _now_iso(),
    })
    _save(reg)
    return _redact(_find(reg, pid))


def update_profile(pid: str, **fields) -> Dict:
    reg = _load()
    p = _find(reg, pid)
    if not p:
        raise ValueError("no such storage profile: %r" % pid)
    for k in ("name", "provider", "endpoint", "region", "bucket", "access_key", "prefix"):
        if fields.get(k) is not None:
            p[k] = str(fields[k]).strip()
    if fields.get("provider") and p["provider"] not in PROVIDERS:
        p["provider"] = "other"
    if "path_style" in fields and fields["path_style"] is not None:
        p["path_style"] = bool(fields["path_style"])
    if "enabled" in fields and fields["enabled"] is not None:
        p["enabled"] = bool(fields["enabled"])
    # empty secret keeps the stored one
    sk = fields.get("secret_key")
    if sk:
        p["secret_key"] = sk
    if not all(p.get(k) for k in _REQUIRED):
        raise ValueError("endpoint, bucket, access_key and secret_key are required")
    _save(reg)
    return _redact(p)


def _dependents(pid: str) -> List[str]:
    """Apps whose schedule targets this profile (so deleting it would orphan them)."""
    try:
        from . import schedule
        return [s["app"] for s in schedule.list_schedules() if pid in (s.get("remotes") or [])]
    except Exception:
        return []


def delete_profile(pid: str, force: bool = False) -> Dict:
    reg = _load()
    p = _find(reg, pid)
    if not p:
        return {"id": pid, "removed": False}
    deps = _dependents(pid)
    if deps and not force:
        return {"id": pid, "removed": False, "in_use_by": deps,
                "reason": "profile is referenced by schedules; pass force to detach"}
    reg["profiles"] = [x for x in reg.get("profiles", []) if x.get("id") != pid]
    _save(reg)
    if deps and force:
        try:
            from . import schedule
            schedule.detach_remote(pid)
        except Exception:
            pass
    return {"id": pid, "removed": True, "detached_from": deps if force else []}


# --------------------------------------------------------------------------- #
# per-profile S3 ops
# --------------------------------------------------------------------------- #
def _client(pid: str) -> s3.S3Client:
    p = _find(_load(), pid)
    if not p or not all(p.get(k) for k in _REQUIRED):
        raise s3.S3Error("storage profile not configured: %r" % pid)
    return s3.S3Client(
        endpoint=p["endpoint"], region=p.get("region", "us-east-1"), bucket=p["bucket"],
        access_key=p["access_key"], secret_key=p["secret_key"],
        prefix=p.get("prefix", ""), path_style=bool(p.get("path_style", True)))


def test_profile(pid: str) -> Dict:
    try:
        _client(pid).head_bucket()
        return {"ok": True, "detail": "bucket reachable"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _resolve_ids(ids) -> List[str]:
    """Normalize a destination selector: None/'' -> [], 'all' -> enabled, csv/list -> ids."""
    if ids in (None, "", []):
        return []
    if isinstance(ids, str):
        if ids.strip().lower() == "all":
            return enabled_ids()
        ids = [x.strip() for x in ids.split(",") if x.strip()]
    return [i for i in ids if i]


def upload(local_path: str, name: str, ids=None) -> Dict:
    """Upload to each selected profile. Returns {"results": {id: {ok,detail}}, "ok_ids":[...]}."""
    results = {}
    ok_ids = []
    for pid in _resolve_ids(ids):
        try:
            _client(pid).put_object(local_path, name)
            results[pid] = {"ok": True, "detail": "uploaded"}
            ok_ids.append(pid)
        except Exception as e:
            results[pid] = {"ok": False, "detail": str(e)}
    return {"results": results, "ok_ids": ok_ids}


def download(name: str, local_path: str, pid: Optional[str] = None) -> Dict:
    """Download from a named profile, or the first enabled profile that has it."""
    candidates = [pid] if pid else enabled_ids()
    last = "no profile holds %r" % name
    for cid in candidates:
        try:
            c = _client(cid)
            if pid is None:  # auto: only try profiles that actually list it
                names = {o.get("name") for o in c.list_objects()}
                if name not in names:
                    continue
            c.get_object(name, local_path)
            return {"ok": True, "detail": local_path, "profile": cid}
        except Exception as e:
            last = str(e)
    return {"ok": False, "detail": last}


def delete(name: str, ids=None) -> Dict:
    """Delete an object from the given profiles (default: all enabled)."""
    targets = _resolve_ids(ids) or enabled_ids()
    removed = []
    for pid in targets:
        try:
            _client(pid).delete_object(name)
            removed.append(pid)
        except Exception:
            pass
    return {"ok": True, "removed_from": removed}


def list_remote(pid: str) -> List[Dict]:
    """Backup objects in ONE profile, as records compatible with store.list_backups
    (app parsed from the name; tagged with the profile id)."""
    out: List[Dict] = []
    try:
        objs = _client(pid).list_objects()
    except Exception:
        return out
    for o in objs:
        n = o.get("name") or ""
        m = _BACKUP_RE.match(n)
        if not m:
            continue
        size = o.get("size") or 0
        out.append({"name": n, "app": m.group("app"), "profile": pid,
                    "size_bytes": size, "size_mb": round(size / (1 << 20), 2)})
    return out


# --------------------------------------------------------------------------- #
# deprecated single-config shims (default profile) — kept one release
# --------------------------------------------------------------------------- #
def get_config(redacted: bool = True) -> Dict:
    profs = list_profiles(redacted=redacted)
    base = profs[0] if profs else {"configured": False, "secret_set": False}
    base = dict(base)
    base["configured"] = configured()
    return base


def set_config(provider, endpoint, region, bucket, access_key, secret_key,
               prefix="", path_style=True) -> Dict:
    reg = _load()
    if _find(reg, "default"):
        return update_profile("default", provider=provider, endpoint=endpoint,
                              region=region, bucket=bucket, access_key=access_key,
                              secret_key=secret_key, prefix=prefix, path_style=path_style)
    return add_profile("default", provider, endpoint, region, bucket, access_key,
                       secret_key, prefix, path_style, pid="default")


def remove() -> Dict:
    return delete_profile("default", force=True)


def test() -> Dict:
    ids = enabled_ids()
    return test_profile(ids[0]) if ids else {"ok": False, "detail": "no storage profile configured"}
