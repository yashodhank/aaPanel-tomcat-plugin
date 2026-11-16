# coding: utf-8
"""
Post-install hardening (closes F9). Applied to a freshly extracted CATALINA_HOME
and to each per-instance CATALINA_BASE. Stricter than the legacy plugin's
"extract stock tarball and run it" behaviour.
"""
from __future__ import annotations

import os
import stat
import re
import shutil
from typing import List

_XML_COMMENT = re.compile(r"<!--.*?-->", re.S)

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
    """Fail if an *active* AJP connector exists. Stock Tomcat ships the AJP
    connector inside a multi-line XML comment, so comments are stripped before
    checking (a naive per-line check false-positives on the commented block)."""
    if not os.path.isfile(server_xml):
        return
    with open(server_xml, "r", errors="replace") as f:
        text = f.read()
    uncommented = _XML_COMMENT.sub("", text)
    if "AJP/1.3" in uncommented:
        raise RuntimeError("active AJP connector detected in %s" % server_xml)


def secure_perms(catalina_base: str, user: str = "www") -> None:
    """Lock down config dir and secrets (best-effort; chown requires privilege)."""
    conf = os.path.join(catalina_base, "conf")
    # conf/ belongs to the app user and this runs as root: chmod through a
    # descriptor opened O_NOFOLLOW so a planted link cannot redirect it.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for _root, _dirs, fns, dir_fd in os.fwalk(conf, follow_symlinks=False):
        for fn in fns:
            mode = 0o600 if fn in ("tomcat-users.xml",) else 0o640
            try:
                fd = os.open(fn, flags, dir_fd=dir_fd)
            except OSError:
                continue
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fchmod(fd, mode)
            except OSError:
                pass
            finally:
                os.close(fd)
