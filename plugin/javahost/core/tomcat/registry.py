# coding: utf-8
"""
Tomcat version model (closes B2/F6 — no hardcoded/stale pins).

Holds the *rules* per major line (min Java, Jakarta namespace, legacy flag) and
resolves the latest patch release dynamically from the Apache download index,
with archive.apache.org as fallback. Verification metadata (.sha512 / .asc /
KEYS) is derived from the resolved URL so installer can fail closed.
"""
from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

DLCDN = "https://dlcdn.apache.org/tomcat"
ARCHIVE = "https://archive.apache.org/dist/tomcat"
KEYS_URL = "https://downloads.apache.org/tomcat/tomcat-{major}/KEYS"


@dataclass(frozen=True)
class TomcatLine:
    major: str        # "9" | "10" | "11"
    line: str         # "9.0" | "10.1" | "11.0"
    min_java: int     # minimum supported Java major
    namespace: str    # "javax" | "jakarta"
    legacy: bool


LINES = {
    "9":  TomcatLine("9", "9.0", 8, "javax", True),
    "10": TomcatLine("10", "10.1", 11, "jakarta", False),
    "11": TomcatLine("11", "11.0", 17, "jakarta", False),
}

# Conservative, known-good fallbacks used ONLY if the live index can't be read.
# Update periodically; integrity is still enforced via .sha512/.asc at install.
_FALLBACK_PATCH = {"9": "9.0.107", "10": "10.1.55", "11": "11.0.22"}

_HREF_RE = re.compile(r'href="v(\d+\.\d+\.\d+)/"')


def get_line(major: str) -> TomcatLine:
    if major not in LINES:
        raise ValueError("unsupported tomcat major: %r" % major)
    return LINES[major]


def matrix():
    """Full Tomcat↔Java compatibility matrix (all lines, installed or not) for the
    UI's beginner guidance. Sorted by major ascending."""
    return [
        {"major": L.major, "line": L.line, "min_java": L.min_java,
         "namespace": L.namespace, "legacy": L.legacy}
        for _, L in sorted(LINES.items(), key=lambda kv: int(kv[0]))
    ]


def recommended():
    """Recommended pairing for a brand-new app: the newest NON-legacy Tomcat line
    and the Java major it requires (today: Tomcat 11 + Java 17). Derived from the
    matrix, not hardcoded, so it tracks future lines."""
    modern = [L for L in LINES.values() if not L.legacy]
    best = max(modern, key=lambda L: int(L.major)) if modern else LINES["11"]
    return {"java": best.min_java, "tomcat": best.major, "line": best.line}


def resolve_latest_patch(major: str, *, timeout: int = 20) -> str:
    """Return newest X.Y.Z for the given major's line, from the live index."""
    line = get_line(major)
    index = "%s/tomcat-%s/" % (DLCDN, major)
    try:
        with urllib.request.urlopen(index, timeout=timeout) as r:  # noqa: S310 (constant Apache URL)
            html = r.read().decode("utf-8", "replace")
        versions = [v for v in _HREF_RE.findall(html) if v.startswith(line.line + ".")]
        if versions:
            return max(versions, key=_ver_key)
    except Exception:
        pass
    return _FALLBACK_PATCH[major]


def _ver_key(v: str):
    return tuple(int(x) for x in v.split("."))


@dataclass(frozen=True)
class TomcatArtifact:
    major: str
    patch: str
    tgz_url: str
    sha512_url: str
    sig_url: str
    keys_url: str
    min_java: int
    namespace: str


def artifact(major: str, patch: Optional[str] = None, *, use_archive: bool = False) -> TomcatArtifact:
    line = get_line(major)
    patch = patch or resolve_latest_patch(major)
    base = ARCHIVE if use_archive else DLCDN
    root = "%s/tomcat-%s/v%s/bin/apache-tomcat-%s.tar.gz" % (base, major, patch, patch)
    return TomcatArtifact(
        major=major,
        patch=patch,
        tgz_url=root,
        sha512_url=root + ".sha512",
        sig_url=root + ".asc",
        keys_url=KEYS_URL.format(major=major),
        min_java=line.min_java,
        namespace=line.namespace,
    )
