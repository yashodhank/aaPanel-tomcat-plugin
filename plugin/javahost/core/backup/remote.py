# coding: utf-8
"""
Remote object-storage config + operations for backups (S3-compatible).

Credentials live in a dedicated 0600 file (remote.json) SEPARATE from the plugin
config.json, and the secret key is NEVER returned to the UI (mirrors the GetDbEnv
secret-safe pattern). All S3 work is delegated to the self-contained client in
s3.py — no third-party deps, custom endpoint (Wasabi/MinIO/B2/R2/AWS).
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from ..util import fs
from . import s3

REMOTE_PATH = "/www/server/javahost/remote.json"
# Known S3-compatible providers (endpoint is still user-supplied/overridable).
PROVIDERS = ("wasabi", "minio", "backblaze", "r2", "aws", "other")
_REQUIRED = ("endpoint", "bucket", "access_key", "secret_key")


def _read() -> Dict:
    try:
        with open(REMOTE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def configured() -> bool:
    c = _read()
    return all(c.get(k) for k in _REQUIRED)


def get_config(redacted: bool = True) -> Dict:
    """Current remote config. With redacted=True (the default, used by the UI) the
    secret key is replaced by a presence flag and never echoed."""
    c = _read()
    out = {
        "provider": c.get("provider", ""),
        "endpoint": c.get("endpoint", ""),
        "region": c.get("region", ""),
        "bucket": c.get("bucket", ""),
        "access_key": c.get("access_key", ""),
        "prefix": c.get("prefix", ""),
        "path_style": bool(c.get("path_style", True)),
        "configured": all(c.get(k) for k in _REQUIRED),
        "secret_set": bool(c.get("secret_key")),
    }
    if not redacted:
        out["secret_key"] = c.get("secret_key", "")
    return out


def set_config(provider: str, endpoint: str, region: str, bucket: str,
               access_key: str, secret_key: str, prefix: str = "",
               path_style: bool = True) -> Dict:
    endpoint = (endpoint or "").strip()
    bucket = (bucket or "").strip()
    access_key = (access_key or "").strip()
    if not (endpoint and bucket and access_key):
        raise ValueError("endpoint, bucket and access_key are required")
    cur = _read()
    # Allow updating non-secret fields without re-entering the secret: if the
    # caller sends an empty secret, keep the stored one.
    secret_key = secret_key if secret_key else cur.get("secret_key", "")
    if not secret_key:
        raise ValueError("secret_key is required")
    if provider and provider not in PROVIDERS:
        provider = "other"
    cfg = {
        "provider": provider or "other",
        "endpoint": endpoint,
        "region": (region or "us-east-1").strip(),
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "prefix": (prefix or "").strip().strip("/"),
        "path_style": bool(path_style),
    }
    fs.ensure_dir(os.path.dirname(REMOTE_PATH))
    fs.atomic_write(REMOTE_PATH, json.dumps(cfg, indent=2) + "\n", mode=0o600)
    return get_config(redacted=True)


def remove() -> Dict:
    removed = False
    if os.path.isfile(REMOTE_PATH):
        os.unlink(REMOTE_PATH)
        removed = True
    return {"removed": removed}


def _client() -> s3.S3Client:
    c = _read()
    if not all(c.get(k) for k in _REQUIRED):
        raise s3.S3Error("remote storage is not configured")
    return s3.S3Client(
        endpoint=c["endpoint"], region=c.get("region", "us-east-1"),
        bucket=c["bucket"], access_key=c["access_key"], secret_key=c["secret_key"],
        prefix=c.get("prefix", ""), path_style=bool(c.get("path_style", True)))


def test() -> Dict:
    try:
        _client().head_bucket()
        return {"ok": True, "detail": "bucket reachable"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def upload(local_path: str, name: str) -> Dict:
    try:
        _client().put_object(local_path, name)
        return {"ok": True, "detail": "uploaded %s" % name}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def download(name: str, local_path: str) -> Dict:
    try:
        _client().get_object(name, local_path)
        return {"ok": True, "detail": local_path}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def delete(name: str) -> Dict:
    try:
        _client().delete_object(name)
        return {"ok": True, "detail": "deleted %s" % name}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def list_remote() -> List[Dict]:
    """Remote backup objects as records compatible with store.list_backups()
    (best-effort; app is parsed from the filename, size from the listing)."""
    import re
    out: List[Dict] = []
    try:
        objs = _client().list_objects()
    except Exception:
        return out
    name_re = re.compile(r"^backup-(?P<app>[A-Za-z0-9._-]+)-\d{8}T\d{6}Z\.tar\.gz$")
    for o in objs:
        n = o.get("name") or ""
        m = name_re.match(n)
        if not m:
            continue
        size = o.get("size") or 0
        out.append({
            "name": n, "app": m.group("app"), "type": None, "domain": None,
            "ssl_enabled": None, "created_at": None,
            "size_bytes": size, "size_mb": round(size / (1 << 20), 2),
            "location": "remote",
        })
    return out
