# coding: utf-8
"""
WAR / exploded-app deployment.

Security: archive extraction is zip-slip-safe (closes the archive-extraction
finding) — every entry is validated to resolve inside the target dir; absolute
paths, `..` traversal and symlink entries are rejected. Also scans WARs for the
javax->jakarta namespace mismatch on Tomcat 10/11 (closes F5).
"""
from __future__ import annotations

import os
import zipfile
from typing import List, Optional

from ..util import fs, download, shell

# Apache Tomcat Migration Tool for Jakarta EE (verified download).
MIGRATION_VER = "1.0.8"
_MIGRATION_BASE = "https://dlcdn.apache.org/tomcat/jakartaee-migration/v%s/binaries"
_MIGRATION_JAR = "jakartaee-migration-%s-shaded.jar"
TOOLS_DIR = "/www/server/javahost/.tools"


class UnsafeArchive(RuntimeError):
    pass


def _safe_target(base: str, name: str) -> str:
    # Reject absolute and drive-style paths up front.
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        raise UnsafeArchive("absolute path in archive: %r" % name)
    target = os.path.realpath(os.path.join(base, name))
    base_real = os.path.realpath(base)
    if target != base_real and not target.startswith(base_real + os.sep):
        raise UnsafeArchive("path traversal in archive: %r" % name)
    return target


def safe_extract(war_path: str, dest_dir: str) -> str:
    """Extract a WAR/zip into dest_dir safely. Returns dest_dir."""
    fs.ensure_dir(dest_dir)
    with zipfile.ZipFile(war_path) as zf:
        for info in zf.infolist():
            # Reject symlinks (high bits of external_attr encode unix mode).
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UnsafeArchive("symlink entry in archive: %r" % info.filename)
            target = _safe_target(dest_dir, info.filename)
            if info.is_dir():
                fs.ensure_dir(target)
                continue
            fs.ensure_dir(os.path.dirname(target))
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
    return dest_dir


def detect_namespace(war_path: str) -> Optional[str]:
    """Return 'javax' or 'jakarta' based on imports/classes in the WAR, or None."""
    try:
        with zipfile.ZipFile(war_path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return None
    has_jakarta = any("jakarta/servlet/" in n for n in names)
    has_javax = any("javax/servlet/" in n for n in names)
    # Class refs live inside .class files; the package dirs above only appear if
    # the app bundles those APIs. Fall back to scanning small descriptor files.
    if not (has_javax or has_jakarta):
        for n in names:
            if n.endswith("web.xml") or n.endswith(".tld"):
                try:
                    with zipfile.ZipFile(war_path) as zf:
                        body = zf.read(n).decode("utf-8", "replace")
                    if "jakarta.servlet" in body or "jakarta.ee" in body:
                        has_jakarta = True
                    if "javax.servlet" in body or "java.sun.com" in body:
                        has_javax = True
                except Exception:
                    pass
    if has_jakarta and not has_javax:
        return "jakarta"
    if has_javax and not has_jakarta:
        return "javax"
    if has_javax and has_jakarta:
        return "mixed"
    return None


def ensure_migration_tool() -> str:
    """Download + SHA-512-verify the Jakarta EE migration JAR. Returns its path."""
    fs.ensure_dir(TOOLS_DIR)
    jar = os.path.join(TOOLS_DIR, _MIGRATION_JAR % MIGRATION_VER)
    if os.path.isfile(jar):
        return jar
    base = _MIGRATION_BASE % MIGRATION_VER
    url = "%s/%s" % (base, _MIGRATION_JAR % MIGRATION_VER)
    # SHA-512 mandatory (fail-closed); Apache publishes .sha512 alongside the jar.
    return download.fetch_verified(url, TOOLS_DIR, sha512_url=url + ".sha512")


def migrate(war_path: str, out_path: str, java_home: str) -> str:
    """Convert a javax.* WAR to jakarta.* using the Apache migration tool.
    Keeps the original (writes a new artifact at out_path). Returns out_path."""
    if not os.path.isfile(war_path):
        raise FileNotFoundError(war_path)
    jar = ensure_migration_tool()
    java = os.path.join(java_home, "bin", "java")
    if not os.access(java, os.X_OK):
        raise RuntimeError("java not executable at %s" % java)
    shell.run([java, "-jar", jar, war_path, out_path])  # arg-list, no shell
    return out_path


def namespace_warning(war_path: str, tomcat_namespace: str) -> Optional[str]:
    """Return a UI warning string if the WAR won't run on the target, else None."""
    ns = detect_namespace(war_path)
    if ns is None:
        return None
    if tomcat_namespace == "jakarta" and ns in ("javax", "mixed"):
        return ("This WAR uses the javax.* namespace, but Tomcat 10/11 require "
                "jakarta.* (Jakarta EE 9+). It will not run as-is. Use the Apache "
                "Tomcat Migration Tool for Jakarta EE, or deploy on Tomcat 9.")
    if tomcat_namespace == "javax" and ns == "jakarta":
        return ("This WAR uses the jakarta.* namespace and requires Tomcat 10+; "
                "it will not run on Tomcat 9.")
    return None
