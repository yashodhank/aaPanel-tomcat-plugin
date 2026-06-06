# coding: utf-8
"""
Input validation. Every value coming from the panel `get` object passes through
here before it touches the filesystem, a command, or a template. Validators
raise ValueError on bad input (fail closed). Closes audit findings F3/F8.
"""
from __future__ import annotations

import re

# A managed app name / identifier: letters, digits, dash, underscore, dot.
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# Hostname per RFC 1123 (labels of a-z0-9-, no leading/trailing dash).
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
SUPPORTED_TOMCAT = {"9", "10", "11"}


def identifier(value: str, field: str = "identifier") -> str:
    value = str(value or "").strip()
    if not _IDENT_RE.match(value):
        raise ValueError("invalid %s: %r" % (field, value))
    if value in (".", ".."):
        raise ValueError("invalid %s: %r" % (field, value))
    return value


def domain(value: str) -> str:
    value = str(value or "").strip().lower()
    if not _DOMAIN_RE.match(value):
        raise ValueError("invalid domain: %r" % value)
    return value


def tomcat_version(value: str) -> str:
    """Normalize 'tomcat10' / '10.1' / '10' -> major key '10'."""
    raw = str(value or "").strip().lower().replace("tomcat", "")
    major = raw.split(".")[0]
    if major not in SUPPORTED_TOMCAT:
        raise ValueError("unsupported tomcat version: %r" % value)
    return major


def port(value, lo: int = 1, hi: int = 65535) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid port: %r" % value)
    if not (lo <= p <= hi):
        raise ValueError("port out of range: %s" % p)
    return p


def java_major(value) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid java version: %r" % value)
    if v not in (8, 11, 17, 21):
        raise ValueError("unsupported java major: %s" % v)
    return v


def memory_mb(value, lo: int = 64, hi: int = 1024 * 1024) -> int:
    try:
        m = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid memory (MB): %r" % value)
    if not (lo <= m <= hi):
        raise ValueError("memory out of range (MB): %s" % m)
    return m
