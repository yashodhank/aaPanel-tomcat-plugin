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
import shutil
import urllib.request
from typing import Optional

from . import shell
from . import fs


_UA = "JavaHost/1.0 (+https://github.com/yashodhank/aaPanel-tomcat-plugin)"


def _http_get(url: str, dest: str, timeout: int = 300) -> None:
    """Download url -> dest. Prefer curl (proxy/retry friendly), fall back to urllib.
    A real User-Agent is sent — some CDNs/APIs (e.g. Adoptium) 403 the default
    python-urllib agent.

    HTTPS only, redirects included: a mirror must not be able to downgrade the
    transfer to plain http."""
    if not str(url or "").startswith("https://"):
        raise ValueError("refusing non-https download: %r" % url)
    curl = shell.which("curl")
    if curl:
        shell.run(
            [curl, "-fSL", "--proto", "=https", "--proto-redir", "=https",
             "-A", _UA, "--retry", "3", "--retry-delay", "5",
             "--max-time", str(timeout), "-o", dest, url],
            timeout=timeout + 30,
        )
        return
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # noqa: S310
        if not r.geturl().startswith("https://"):
            raise RuntimeError("download redirected off https: %s" % r.geturl())
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
    expected = ((expected_hex or "").split() or [""])[0].lower()
    if actual.lower() != expected:
        raise RuntimeError(
            "SHA-512 mismatch for %s\n  expected %s\n  actual   %s"
            % (os.path.basename(path), expected, actual)
        )


def verify_gpg(path: str, sig_path: str, keyring: str) -> None:
    """Verify a detached signature against a keyring. Raises if gpg is missing."""
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
      - sig_url -> the OpenPGP signature is REQUIRED too, so `keyring` must be
        given. A missing keyring is an error, never a silent skip.

    A download lands under a temporary name and is only renamed to its final
    name after every check passed, so a path returned by (or left behind by)
    this function is always a verified artifact. A failed or interrupted
    download leaves nothing behind.
    """
    fs.ensure_dir(dest_dir)
    name = os.path.basename(url.split("?")[0]) or "artifact"
    if sig_url and not keyring:
        raise RuntimeError("no OpenPGP keyring for %s; refusing to skip signature "
                           "verification" % name)
    if not (sha512 or sha512_url):
        raise RuntimeError("no SHA-512 provided for %s; refusing to use unverified artifact" % name)

    work = fs.mkdtemp_in(dest_dir, ".fetch-")
    try:
        path = local_file or os.path.join(work, name)
        if not local_file:
            _http_get(url, path)

        expected = sha512
        if not expected:
            sums = os.path.join(work, name + ".sha512")
            _http_get(sha512_url, sums)
            with open(sums, "r") as f:
                expected = f.read()
        verify_sha512(path, expected)

        if sig_url:
            sig = os.path.join(work, name + ".asc")
            _http_get(sig_url, sig)
            verify_gpg(path, sig, keyring)

        if local_file:
            return local_file
        dest = os.path.join(dest_dir, name)
        os.replace(path, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)
