# coding: utf-8
"""
Verified downloads (closes audit finding F2 — supply-chain integrity).

Every artifact is verified by SHA-512 and, when available, OpenPGP signature
before it is used. Verification is fail-closed: an unverifiable artifact is an
error, never a silent skip. Supports an offline path (local file + expected
hash) so air-gapped installs are possible without weakening integrity.
"""
from __future__ import annotations

import hashlib
import os
import urllib.request
from typing import Optional

from . import shell
from . import fs


def _http_get(url: str, dest: str, timeout: int = 300) -> None:
    """Download url -> dest. Prefer curl (proxy/retry friendly), fall back to urllib."""
    curl = shell.which("curl")
    if curl:
        shell.run(
            [curl, "-fSL", "--retry", "3", "--retry-delay", "5",
             "--max-time", str(timeout), "-o", dest, url],
            timeout=timeout + 30,
        )
        return
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:  # noqa: S310
        # url is composed from our own version registry (https Apache/Adoptium), not user input
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


def sha512_file(path: str) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha512(path: str, expected_hex: str) -> None:
    actual = sha512_file(path)
    expected = (expected_hex or "").strip().split()[0].lower()
    if actual.lower() != expected:
        raise RuntimeError(
            "SHA-512 mismatch for %s\n  expected %s\n  actual   %s"
            % (os.path.basename(path), expected, actual)
        )


def verify_gpg(path: str, sig_path: str, keyring: str) -> None:
    """Verify detached signature against a keyring. No-op-with-warning only if
    gpg is unavailable AND caller already verified sha512 (decided by caller)."""
    gpg = shell.which("gpg")
    if not gpg:
        raise RuntimeError("gpg not installed; cannot verify OpenPGP signature for %s" % path)
    shell.run([gpg, "--no-default-keyring", "--keyring", keyring, "--verify", sig_path, path])


def fetch_verified(
    url: str,
    dest_dir: str,
    *,
    sha512: Optional[str] = None,
    sha512_url: Optional[str] = None,
    sig_url: Optional[str] = None,
    keyring: Optional[str] = None,
    local_file: Optional[str] = None,
) -> str:
    """Obtain an artifact and verify it. Returns the local path.

    Resolution order for the artifact:
      1. local_file (offline mode), else
      2. download from url.
    Verification (must satisfy at least SHA-512):
      - sha512 (explicit hex) OR sha512_url (download the .sha512), required.
      - sig_url + keyring -> additionally verify OpenPGP signature.
    """
    fs.ensure_dir(dest_dir)
    name = os.path.basename(url.split("?")[0]) or "artifact"
    dest = local_file if local_file else os.path.join(dest_dir, name)

    if not local_file:
        _http_get(url, dest)

    expected = sha512
    if not expected and sha512_url:
        sums = os.path.join(dest_dir, name + ".sha512")
        _http_get(sha512_url, sums)
        with open(sums, "r") as f:
            expected = f.read()
    if not expected:
        raise RuntimeError("no SHA-512 provided for %s; refusing to use unverified artifact" % name)
    verify_sha512(dest, expected)

    if sig_url and keyring:
        sig = os.path.join(dest_dir, name + ".asc")
        _http_get(sig_url, sig)
        verify_gpg(dest, sig, keyring)

    return dest
