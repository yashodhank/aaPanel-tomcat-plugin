# coding: utf-8
"""
JVM option validation (closes F10).

Strips flags removed/unsupported on modern JVMs so a Java-8-era option set does
not prevent Tomcat from starting on Java 17/21. Returns the cleaned flag list
plus a list of human-readable warnings for the UI.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# Flags removed or no-op'd on Java 11+ (mostly the CMS collector & PermGen).
_REMOVED_11_PLUS = {
    "-XX:+UseConcMarkSweepGC",
    "-XX:-UseConcMarkSweepGC",
    "-XX:+CMSIncrementalMode",
    "-XX:+UseParNewGC",
    "-Xincgc",
}
# Prefix flags removed after Java 8 (PermGen sizing).
_REMOVED_PREFIX_8 = ("-XX:PermSize=", "-XX:MaxPermSize=", "-XX:+CMS", "-XX:CMS")

# Allow only sane characters; reject anything that looks like shell injection.
_SAFE_OPT = re.compile(r"^[A-Za-z0-9_:+\-=.,/%@${}]+$")


def sanitize(opts: List[str], java_major: int) -> Tuple[List[str], List[str]]:
    cleaned: List[str] = []
    warnings: List[str] = []
    for opt in opts:
        opt = (opt or "").strip()
        if not opt:
            continue
        if not _SAFE_OPT.match(opt):
            warnings.append("dropped unsafe JVM option: %r" % opt)
            continue
        if java_major >= 11 and opt in _REMOVED_11_PLUS:
            warnings.append("removed flag unsupported on Java %d: %s" % (java_major, opt))
            continue
        if java_major >= 9 and opt.startswith(_REMOVED_PREFIX_8):
            warnings.append("removed flag unsupported on Java %d: %s" % (java_major, opt))
            continue
        cleaned.append(opt)
    return cleaned, warnings


def default_opts(heap_mb: int) -> List[str]:
    """Conservative, modern-JVM-safe defaults."""
    return [
        "-server",
        "-Xms%dm" % max(64, heap_mb // 2),
        "-Xmx%dm" % heap_mb,
        "-XX:+UseG1GC",
        "-Djava.security.egd=file:/dev/urandom",
        "-Dfile.encoding=UTF-8",
    ]
