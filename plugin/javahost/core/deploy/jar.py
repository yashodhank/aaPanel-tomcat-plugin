# coding: utf-8
"""
Executable / Spring Boot fat-JAR support.

Some Java apps ship as a self-contained executable JAR (Spring Boot, Quarkus
uber-jar, Micronaut) rather than a WAR for Tomcat. JavaHost runs these directly
as a `java -jar` service. This module only inspects the jar; the service wiring
lives in tomcat/service.py and the lifecycle in tomcat/instance.py.
"""
from __future__ import annotations

import zipfile
from typing import Optional


def manifest_main_class(jar_path: str) -> Optional[str]:
    """Return the Main-Class from META-INF/MANIFEST.MF, or None."""
    try:
        with zipfile.ZipFile(jar_path) as z:
            mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, FileNotFoundError):
        return None
    # MANIFEST values can be line-folded (continuation lines start with a space).
    joined = mf.replace("\r\n", "\n").replace("\n ", "")
    for line in joined.split("\n"):
        if line.startswith("Main-Class:"):
            return line.split(":", 1)[1].strip()
    return None


def is_executable_jar(jar_path: str) -> bool:
    """True if the jar declares a Main-Class (runnable via `java -jar`)."""
    return manifest_main_class(jar_path) is not None


def detect_springboot(jar_path: str) -> bool:
    """True if the jar looks like a Spring Boot fat-jar."""
    mc = manifest_main_class(jar_path) or ""
    if "springframework.boot.loader" in mc:
        return True
    try:
        with zipfile.ZipFile(jar_path) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, FileNotFoundError):
        return False
    return any(n.startswith("BOOT-INF/") for n in names) or \
        any(n.startswith("org/springframework/boot/loader/") for n in names)
