# coding: utf-8
"""
Minimal, dependency-free S3-compatible client (AWS SigV4) for pushing backups to
remote object storage — Wasabi, MinIO, Backblaze B2, Cloudflare R2, AWS S3.

WHY self-contained (no boto3): the plugin ships no third-party runtime deps and
boto3 isn't guaranteed on a panel box; aaPanel's own S3 backend is coupled to its
backup cron and hard-codes AWS regions (no custom endpoint → no Wasabi). A ~200-line
stdlib SigV4 signer over http.client supports ANY S3 endpoint via `endpoint_url`.

Scope: single PUT (≤5 GB), GET, HEAD-bucket, ListObjectsV2, DELETE. Uploads stream
the file with `x-amz-content-sha256: UNSIGNED-PAYLOAD` over TLS, so we never hash
the whole archive. Multipart (>5 GB) is intentionally out of scope (documented).
Path-style addressing by default (works for MinIO/Wasabi/AWS).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import ssl as _sslmod
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_UNSIGNED = "UNSIGNED-PAYLOAD"


class S3Error(RuntimeError):
    pass


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret: str, datestamp: str, region: str, service: str = "s3") -> bytes:
    """AWS SigV4 signing-key derivation (exposed for unit testing against the
    documented AWS vector)."""
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


class S3Client:
    def __init__(self, endpoint: str, region: str, bucket: str, access_key: str,
                 secret_key: str, prefix: str = "", path_style: bool = True,
                 timeout: float = 120.0):
        if not (endpoint and bucket and access_key and secret_key):
            raise S3Error("endpoint, bucket, access_key and secret_key are required")
        u = urllib.parse.urlparse(endpoint if "://" in endpoint else "https://" + endpoint)
        self.scheme = u.scheme or "https"
        self.endpoint_host = u.netloc
        self.region = region or "us-east-1"
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.prefix = (prefix or "").strip("/")
        self.path_style = path_style
        self.timeout = timeout

    # --- key / uri helpers ---
    def full_key(self, name: str) -> str:
        return ("%s/%s" % (self.prefix, name)).lstrip("/") if self.prefix else name

    def _host(self) -> str:
        return self.endpoint_host if self.path_style else "%s.%s" % (self.bucket, self.endpoint_host)

    def _uri(self, key: str) -> str:
        if self.path_style:
            parts = [self.bucket] + (key.split("/") if key else [])
        else:
            parts = key.split("/") if key else []
        return "/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)

    @staticmethod
    def _canon_qs(query: Optional[Dict[str, str]]) -> str:
        if not query:
            return ""
        items = sorted(query.items())
        return "&".join("%s=%s" % (urllib.parse.quote(k, safe="~"),
                                   urllib.parse.quote(str(v), safe="~")) for k, v in items)

    def _sign(self, method: str, key: str, query: Optional[Dict[str, str]],
              payload_hash: str) -> Dict[str, str]:
        now = datetime.datetime.now(datetime.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        host = self._host()
        uri = self._uri(key)
        canon_qs = self._canon_qs(query)
        canon_headers = "host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n" % (host, payload_hash, amzdate)
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canon_req = "\n".join([method, uri, canon_qs, canon_headers, signed_headers, payload_hash])
        scope = "%s/%s/s3/aws4_request" % (datestamp, self.region)
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                         hashlib.sha256(canon_req.encode("utf-8")).hexdigest()])
        signing_key = derive_signing_key(self.secret_key, datestamp, self.region, "s3")
        signature = hmac.new(signing_key, sts.encode("utf-8"), hashlib.sha256).hexdigest()
        auth = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                % (self.access_key, scope, signed_headers, signature))
        return {"Host": host, "x-amz-date": amzdate,
                "x-amz-content-sha256": payload_hash, "Authorization": auth}

    def _conn(self):
        host = self._host()
        if self.scheme == "http":
            return http.client.HTTPConnection(host, timeout=self.timeout)
        return http.client.HTTPSConnection(host, timeout=self.timeout,
                                           context=_sslmod.create_default_context())

    def _path(self, key: str, query: Optional[Dict[str, str]]) -> str:
        uri = self._uri(key)
        qs = self._canon_qs(query)
        return uri + ("?" + qs if qs else "")

    # --- operations ---
    def head_bucket(self) -> None:
        """Raise S3Error unless the bucket is reachable + credentials valid."""
        headers = self._sign("HEAD", "", None, _EMPTY_SHA)
        conn = self._conn()
        try:
            conn.request("HEAD", self._path("", None), headers=headers)
            resp = conn.getresponse()
            resp.read()
            if resp.status not in (200, 204):
                raise S3Error("HEAD bucket failed: HTTP %d" % resp.status)
        finally:
            conn.close()

    def put_object(self, local_path: str, name: str) -> None:
        import os
        key = self.full_key(name)
        size = os.path.getsize(local_path)
        headers = self._sign("PUT", key, None, _UNSIGNED)
        headers["Content-Length"] = str(size)
        headers["Content-Type"] = "application/gzip"
        conn = self._conn()
        try:
            with open(local_path, "rb") as f:
                conn.request("PUT", self._path(key, None), body=f, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
            if resp.status not in (200, 201):
                raise S3Error("PUT failed: HTTP %d %s" % (resp.status, body[:300].decode("utf-8", "replace")))
        finally:
            conn.close()

    def get_object(self, name: str, local_path: str) -> None:
        key = self.full_key(name)
        headers = self._sign("GET", key, None, _EMPTY_SHA)
        conn = self._conn()
        try:
            conn.request("GET", self._path(key, None), headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read()
                raise S3Error("GET failed: HTTP %d" % resp.status)
            with open(local_path, "wb") as out:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
        finally:
            conn.close()

    def delete_object(self, name: str) -> None:
        key = self.full_key(name)
        headers = self._sign("DELETE", key, None, _EMPTY_SHA)
        conn = self._conn()
        try:
            conn.request("DELETE", self._path(key, None), headers=headers)
            resp = conn.getresponse()
            resp.read()
            if resp.status not in (200, 202, 204):
                raise S3Error("DELETE failed: HTTP %d" % resp.status)
        finally:
            conn.close()

    def list_objects(self, prefix: Optional[str] = None) -> List[Dict]:
        """ListObjectsV2 under the client prefix (optionally narrowed). Returns
        [{key, name, size}] (name = key with the client prefix stripped)."""
        q = {"list-type": "2"}
        eff_prefix = self.prefix
        if prefix:
            eff_prefix = ("%s/%s" % (self.prefix, prefix)).lstrip("/") if self.prefix else prefix
        if eff_prefix:
            q["prefix"] = eff_prefix
        headers = self._sign("GET", "", q, _EMPTY_SHA)
        conn = self._conn()
        try:
            conn.request("GET", self._path("", q), headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise S3Error("LIST failed: HTTP %d %s" % (resp.status, data[:300].decode("utf-8", "replace")))
        finally:
            conn.close()
        return self._parse_list(data)

    def _parse_list(self, data: bytes) -> List[Dict]:
        out: List[Dict] = []
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return out
        plen = len(self.prefix) + 1 if self.prefix else 0
        for el in root.iter():
            if not el.tag.endswith("}Contents") and el.tag != "Contents":
                continue
            key = size = None
            for ch in el:
                if ch.tag.endswith("}Key") or ch.tag == "Key":
                    key = ch.text
                elif ch.tag.endswith("}Size") or ch.tag == "Size":
                    try:
                        size = int(ch.text)
                    except (TypeError, ValueError):
                        size = None
            if key:
                out.append({"key": key, "name": key[plen:] if plen and key.startswith(self.prefix + "/") else key,
                            "size": size})
        return out
