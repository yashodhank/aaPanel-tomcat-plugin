# coding: utf-8
"""
Post-install hardening (closes F9). Applied to a freshly extracted CATALINA_HOME
and to each per-instance CATALINA_BASE. Stricter than the legacy plugin's
"extract stock tarball and run it" behaviour.
"""
from __future__ import annotations

import os
import shutil
from typing import List

# Bundled webapps to remove unless explicitly opted in.
_DROP_WEBAPPS = ["examples", "docs", "host-manager"]
# 'manager' is removed by default too; enabling it is a deliberate, gated action.
_DROP_WEBAPPS_DEFAULT_MANAGER = ["manager"]


def harden_home(catalina_home: str, *, keep_manager: bool = False) -> List[str]:
    """Remove risky bundled webapps. Returns list of removed paths."""
    removed = []
    targets = list(_DROP_WEBAPPS)
    if not keep_manager:
        targets += _DROP_WEBAPPS_DEFAULT_MANAGER
    webapps = os.path.join(catalina_home, "webapps")
    for name in targets:
        p = os.path.join(webapps, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
            removed.append(p)
    return removed


def assert_no_ajp(server_xml: str) -> None:
    """Fail if an AJP connector is active (defense in depth; our template omits it)."""
    if not os.path.isfile(server_xml):
        return
    with open(server_xml, "r", errors="replace") as f:
        text = f.read()
    for line in text.splitlines():
        s = line.strip()
        if "AJP/1.3" in s and not s.startswith("<!--"):
            raise RuntimeError("active AJP connector detected in %s" % server_xml)


def secure_perms(catalina_base: str, user: str = "www") -> None:
    """Lock down config dir and secrets (best-effort; chown requires privilege)."""
    conf = os.path.join(catalina_base, "conf")
    for root, dirs, fns in os.walk(conf):
        for fn in fns:
            p = os.path.join(root, fn)
            mode = 0o600 if fn in ("tomcat-users.xml",) else 0o640
            try:
                os.chmod(p, mode)
            except OSError:
                pass
