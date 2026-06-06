# coding: utf-8
"""
Tomcat install orchestrator.

Flow per major line:
  resolve artifact -> verified download (sha512 + gpg) -> atomic staged extract
  -> harden -> mark managed. Java floor is enforced before anything is written.
Rollback: everything lands in a .staging path and is atomically moved into place
only on success; failures discard staging and leave existing installs intact.
Closes F2 (integrity), F9 (hardening), F12 (disk precheck), F14 (managed marker),
and the rollback gap (audit §8).
"""
from __future__ import annotations

import os
from typing import Optional

from . import registry, hardening
from ..util import download, fs, shell
from ..runtime import java

HOME_ROOT = "/www/server/javahost/tomcat"          # shared CATALINA_HOME per major
KEYRING_DIR = "/www/server/javahost/.keys"


def home_path(major: str) -> str:
    return os.path.join(HOME_ROOT, major)


def is_installed(major: str) -> Optional[str]:
    """Return installed patch version, or None."""
    marker = os.path.join(home_path(major), "RELEASE-NOTES")
    ver_file = os.path.join(home_path(major), ".javahost-version")
    if os.path.isfile(ver_file):
        with open(ver_file) as f:
            return f.read().strip()
    return None if not os.path.exists(marker) else "unknown"


def ensure_java(major: str, prefer: Optional[int] = None) -> str:
    """Return a JAVA_HOME satisfying the line's floor; install if needed."""
    line = registry.get_line(major)
    home = java.resolve(line.min_java, prefer=prefer)
    if home:
        return home
    # Auto-install the lowest modern JDK that satisfies the floor.
    want = 17 if line.min_java <= 17 else 21
    if line.min_java > 17:
        want = 21
    return java.install_temurin(want)


def install(major: str, *, patch: Optional[str] = None, prefer_java: Optional[int] = None,
            keep_manager: bool = False, local_tarball: Optional[str] = None,
            local_sha512: Optional[str] = None) -> dict:
    major = registry.get_line(major).major  # validates
    java_home = ensure_java(major, prefer=prefer_java)
    art = registry.artifact(major, patch)

    dest = home_path(major)
    staging = dest + ".staging"
    tmp = fs.mkdtemp("javahost-tc-")
    try:
        fs.require_free(HOME_ROOT if os.path.isdir(HOME_ROOT) else "/www", 300 * (1 << 20))
        fs.ensure_dir(KEYRING_DIR, 0o700)

        # 1. verified download (offline path supported)
        tgz = download.fetch_verified(
            art.tgz_url, tmp,
            sha512=local_sha512,
            sha512_url=None if local_tarball else art.sha512_url,
            sig_url=None if local_tarball else art.sig_url,
            keyring=_keyring(major, art.keys_url),
            local_file=local_tarball,
        )

        # 2. staged extract
        if os.path.isdir(staging):
            fs.safe_rmtree(staging, require_marker=False)
        fs.ensure_dir(staging)
        shell.run(["tar", "-xzf", tgz, "--strip-components=1", "-C", staging])

        # 3. harden + version marker
        hardening.harden_home(staging, keep_manager=keep_manager)
        fs.atomic_write(os.path.join(staging, ".javahost-version"), art.patch + "\n", 0o644)
        fs.mark_managed(staging)

        # 4. atomic swap
        if os.path.isdir(dest):
            old = dest + ".old"
            if os.path.isdir(old):
                fs.safe_rmtree(old, require_marker=False)
            os.rename(dest, old)
            os.rename(staging, dest)
            fs.safe_rmtree(old, require_marker=False)
        else:
            os.rename(staging, dest)
    except Exception:
        if os.path.isdir(staging):
            try:
                fs.safe_rmtree(staging, require_marker=False)
            except Exception:
                pass
        raise
    finally:
        if tmp.startswith("/tmp"):
            try:
                fs.safe_rmtree(tmp, require_marker=False)
            except Exception:
                pass
    return {"major": major, "patch": art.patch, "home": dest, "java_home": java_home}


def uninstall(major: str) -> None:
    major = registry.get_line(major).major
    dest = home_path(major)
    if os.path.isdir(dest):
        fs.safe_rmtree(dest, require_marker=True)  # refuses unmanaged (F14)


def _keyring(major: str, keys_url: str) -> Optional[str]:
    """Build a GPG keyring from Apache KEYS for this major. Returns path or None
    (None makes the verifier skip the signature step but still require sha512)."""
    gpg = shell.which("gpg")
    if not gpg:
        return None
    keys = os.path.join(KEYRING_DIR, "tomcat-%s-KEYS" % major)
    keyring = os.path.join(KEYRING_DIR, "tomcat-%s.gpg" % major)
    try:
        download._http_get(keys_url, keys)
        shell.run([gpg, "--no-default-keyring", "--keyring", keyring, "--import", keys])
        return keyring
    except Exception:
        return None
