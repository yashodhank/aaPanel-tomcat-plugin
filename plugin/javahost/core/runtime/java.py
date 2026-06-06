# coding: utf-8
"""
Java runtime layer (closes F10).

- Detect installed JDKs across known locations + PATH.
- Robustly parse `java -version` across vendor banners.
- Install Temurin JDK 17 and 21 (verified) when missing.
- Expose per-runtime JAVA_HOME; never mutates system-wide alternatives silently.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from ..util import shell, download, fs

# Managed JDK install root (per-runtime, isolated from the OS).
JDK_ROOT = "/www/server/javahost/runtimes"

# Search order for already-present JDKs (newest first).
_SEARCH = [
    "/www/server/javahost/runtimes/jdk-21",
    "/www/server/javahost/runtimes/jdk-17",
    "/www/server/javahost/runtimes/jdk-11",
    "/www/server/javahost/runtimes/jdk-8",
    "/usr/local/btjdk/jdk21",
    "/usr/local/btjdk/jdk17",
    "/usr/local/btjdk/jdk11",
    "/usr/local/btjdk/jdk8",
    "/usr/lib/jvm",  # distro JDKs (expanded below)
]

# Matches: version "1.8.0_402"  |  version "11.0.22"  |  version "17.0.10"  |  openjdk 21 2024-...
_VER_RE = re.compile(r'version "?(\d+)(?:\.(\d+))?(?:\.(\d+))?', re.I)
_OPENJDK_RE = re.compile(r"openjdk\s+(\d+)", re.I)


def parse_major(version_output: str) -> Optional[int]:
    """Return the Java *major* (8, 11, 17, 21...) from `java -version` text."""
    if not version_output:
        return None
    m = _VER_RE.search(version_output)
    if m:
        a, b = int(m.group(1)), int(m.group(2) or 0)
        # Legacy scheme: 1.8 -> 8 ; modern: first component is the major.
        return b if a == 1 else a
    m = _OPENJDK_RE.search(version_output)
    return int(m.group(1)) if m else None


def probe(java_home: str) -> Optional[int]:
    """Return the major version of the JDK at java_home, or None."""
    java_bin = os.path.join(java_home, "bin", "java")
    if not os.access(java_bin, os.X_OK):
        return None
    rc, out, err = shell.run([java_bin, "-version"], check=False)
    return parse_major((err or "") + (out or ""))


def _expand_candidates() -> List[str]:
    out: List[str] = []
    for c in _SEARCH:
        if c == "/usr/lib/jvm" and os.path.isdir(c):
            out += [os.path.join(c, d) for d in sorted(os.listdir(c), reverse=True)]
        else:
            out.append(c)
    return out


def detect() -> Dict[int, str]:
    """Map of major -> java_home for every JDK found (first wins per major)."""
    found: Dict[int, str] = {}
    for home in _expand_candidates():
        real = os.path.realpath(home)
        major = probe(real)
        if major and major not in found:
            found[major] = real
    # Also consider `java` on PATH.
    java = shell.which("java")
    if java:
        home = os.path.dirname(os.path.dirname(os.path.realpath(java)))
        major = probe(home)
        if major and major not in found:
            found[major] = home
    return found


def resolve(min_major: int, prefer: Optional[int] = None) -> Optional[str]:
    """Pick a JAVA_HOME satisfying >= min_major, preferring `prefer` if present."""
    found = detect()
    if prefer and prefer in found and prefer >= min_major:
        return found[prefer]
    for major in sorted(found, reverse=True):
        if major >= min_major:
            return found[major]
    return None


# --- Temurin install metadata (verified via Adoptium API at runtime) ---
_ADOPTIUM_API = "https://api.adoptium.net/v3"
# Adoptium's API returns HTTP 403 for the default Python-urllib User-Agent; send
# a real one on the metadata request.
_UA = "JavaHost/1.0 (+https://github.com/yashodhank/aaPanel-tomcat-plugin)"


def install_temurin(major: int, *, arch: str = "x64", os_name: str = "linux") -> str:
    """Download + verify + extract Temurin JDK <major>. Returns JAVA_HOME."""
    if major not in (17, 21, 11, 8):
        raise ValueError("unsupported JDK major to install: %s" % major)
    import json as _json
    import urllib.request

    api = ("%s/assets/latest/%d/hotspot?architecture=%s&image_type=jdk&os=%s"
           % (_ADOPTIUM_API, major, arch, os_name))
    req = urllib.request.Request(api, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (constant Adoptium URL)
        assets = _json.load(r)
    if not assets:
        raise RuntimeError("Adoptium returned no JDK %d asset" % major)
    pkg = assets[0]["binary"]["package"]
    url = pkg["link"]
    sha = pkg.get("checksum")  # Adoptium provides sha256; we also accept .sha256.txt
    if not sha:
        sha_url = pkg.get("checksum_link")
    else:
        sha_url = None

    tmp = fs.mkdtemp("javahost-jdk-")
    try:
        # Adoptium publishes SHA-256; verify_sha512 expects sha512, so verify sha256 here.
        tgz = _fetch_with_sha256(url, tmp, sha, sha_url)
        dest = os.path.join(JDK_ROOT, "jdk-%d" % major)
        fs.ensure_dir(JDK_ROOT)
        fs.require_free(JDK_ROOT, 400 * (1 << 20))
        if os.path.isdir(dest):
            fs.safe_rmtree(dest, require_marker=False)
        fs.ensure_dir(dest)
        shell.run(["tar", "-xzf", tgz, "--strip-components=1", "-C", dest])
        fs.mark_managed(dest)
    finally:
        # `tmp` is our own fs.mkdtemp (0700, in the system tempdir). Remove it
        # directly: safe_rmtree only permits MANAGED_ROOTS and would refuse /tmp.
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)
    got = probe(dest)
    if got != major:
        raise RuntimeError("installed JDK reports major %s, expected %s" % (got, major))
    return dest


# --- uninstall / dependents (Feature 1) ---
# Where the plugin's own runtimes live. ANYTHING outside this tree (notably the
# panel-owned /usr/local/btjdk) is shared/system and must never be removed.
_PLUGIN_RUNTIMES = JDK_ROOT  # "/www/server/javahost/runtimes"


def _is_plugin_runtime(path: str) -> bool:
    """True only when `path` is a plugin-managed runtime under JDK_ROOT."""
    if not path:
        return False
    rp = os.path.realpath(path)
    root = os.path.realpath(_PLUGIN_RUNTIMES)
    return rp == root or rp.startswith(root + os.sep)


def usage(major: int) -> List[str]:
    """App names whose pinned JAVA_HOME resolves to Java `major`.

    Scans every instance's setenv.sh / app.env for a JAVA_HOME and compares its
    parsed major to `major`. Defensive: a malformed instance never raises here.
    Imported lazily to avoid a runtime<->tomcat import cycle.
    """
    from ..tomcat import instance as _inst

    out: List[str] = []
    root = _inst.INSTANCE_ROOT
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        base = os.path.join(root, name)
        if not os.path.isdir(base):
            continue
        try:
            env = _inst._read_setenv(base)
            jhome = env.get("JAVA_HOME") or _inst._read_app_env(base).get("JAVA_HOME", "")
            if _inst._java_major_from_home(jhome) == int(major):
                out.append(name)
        except Exception:
            continue
    return out


def uninstall(major: int, force: bool = False) -> Dict:
    """Remove the plugin-managed JDK `major`.

    Refuses the panel-owned JDK (anything outside /www/server/javahost/runtimes):
    those are shared; reinstall into the plugin dir instead of deleting the
    panel's copy. Blocks (not silently) when any app pins this major unless
    `force=True`. Returns a summary dict; never removes anything system-wide.
    """
    major = int(major)
    found = detect()
    home = found.get(major)
    if not home:
        # nothing detected for this major; treat the canonical plugin path
        dest = os.path.join(_PLUGIN_RUNTIMES, "jdk-%d" % major)
        if not os.path.isdir(dest):
            return {"major": major, "removed": False, "reason": "not installed"}
        home = dest
    if not _is_plugin_runtime(home):
        raise RuntimeError(
            "Java %d resolves to a panel-managed JDK (%s, shared); refusing to "
            "remove it. Reinstall into the plugin dir instead, don't remove the "
            "panel's copy." % (major, os.path.realpath(home)))
    in_use = usage(major)
    if in_use and not force:
        raise RuntimeError(
            "Java %d is in use by: %s (pass force to remove anyway)"
            % (major, ", ".join(in_use)))
    dest = os.path.join(_PLUGIN_RUNTIMES, "jdk-%d" % major)
    target = dest if os.path.isdir(dest) else os.path.realpath(home)
    removed = False
    if os.path.isdir(target):
        # marker-gated when present (install_temurin marks it), else a guarded
        # rmtree of exactly that dir (safe_rmtree still refuses anything outside
        # the managed roots / follows-symlink-out protection).
        fs.safe_rmtree(target, require_marker=fs.is_managed(target))
        removed = True
    return {"major": major, "removed": removed, "home": target,
            "in_use_by": in_use, "forced": bool(force)}


def reinstall(major: int, to_plugin_dir: bool = False) -> Dict:
    """Reinstall Temurin JDK `major` into the plugin runtimes.

    Normally uninstalls the plugin-managed copy first (force=True) then installs.
    For a panel-owned JDK (e.g. the panel's jdk17) `to_plugin_dir=True` means:
    DON'T touch the panel copy — just install_temurin(major) under the plugin
    runtimes so the plugin owns its own. With to_plugin_dir False on a panel
    path, uninstall() refuses (by design) and the error surfaces.
    """
    major = int(major)
    found = detect()
    home = found.get(major)
    panel_owned = bool(home) and not _is_plugin_runtime(home)
    if panel_owned and to_plugin_dir:
        # leave the panel's copy alone; give the plugin its own under JDK_ROOT
        new_home = install_temurin(major)
        return {"major": major, "reinstalled": True, "home": new_home,
                "kept_panel_copy": True}
    uninstall(major, force=True)
    new_home = install_temurin(major)
    return {"major": major, "reinstalled": True, "home": new_home,
            "kept_panel_copy": False}


def _fetch_with_sha256(url: str, dest_dir: str, sha256_hex, sha256_url) -> str:
    import hashlib
    name = os.path.basename(url.split("?")[0])
    dest = os.path.join(dest_dir, name)
    download._http_get(url, dest)  # internal reuse of the curl/urllib helper
    expected = (sha256_hex or "").strip()
    if not expected and sha256_url:
        sums = dest + ".sha256.txt"
        download._http_get(sha256_url, sums)
        with open(sums) as f:
            expected = f.read().split()[0]
    if not expected:
        raise RuntimeError("no SHA-256 for JDK; refusing unverified artifact")
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    if h.hexdigest().lower() != expected.lower():
        raise RuntimeError("SHA-256 mismatch for %s" % name)
    return dest
