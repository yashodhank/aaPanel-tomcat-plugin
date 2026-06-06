#!/usr/bin/env python3
# coding: utf-8
"""
CI helper: install a Tomcat major via the plugin's *real* verified-download
installer (sha512 + optional GPG), with an explicit fallback to the Apache
archive mirror if the primary dlcdn mirror is flaky.

The plugin's installer.install() always builds artifact URLs from registry.DLCDN
and does not expose a use_archive switch, so we point DLCDN at the archive host
on the retry. Verification stays fully enforced on both attempts. Designed to run
on a GitHub-hosted Ubuntu runner where /www/server/javahost already exists and is
owned by the runner user, and JAVA_HOME points at a setup-java Temurin JDK.

Usage:  JAVAHOST_PLUGIN_DIR=.../plugin/javahost python3 install_tomcat.py [major]
        default major = 11. Exit 0 = installed.
"""
import os
import sys

PLUGIN = os.environ.get("JAVAHOST_PLUGIN_DIR", "/www/server/panel/plugin/javahost")
for p in (PLUGIN, "/www/server/panel/class", "/www/server/panel"):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.tomcat import installer, registry  # noqa: E402


def main():
    major = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JAVAHOST_E2E_TOMCAT", "11")
    prefer = None
    jh = os.environ.get("JAVA_HOME")
    if jh:
        # Let the run use the setup-java JDK instead of downloading one.
        from core.runtime import java
        prefer = java.probe(jh)
        print("JAVA_HOME=%s (major=%s)" % (jh, prefer))

    if installer.is_installed(major):
        print("Tomcat %s already installed: %s" % (major, installer.is_installed(major)))
        return 0

    try:
        res = installer.install(major, prefer_java=prefer)
        print("installed via dlcdn:", res)
        return 0
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[install_tomcat] dlcdn install failed (%s); retrying archive mirror\n" % e)

    # Retry against archive.apache.org by repointing the registry's download host.
    registry.DLCDN = registry.ARCHIVE
    res = installer.install(major, prefer_java=prefer)
    print("installed via archive:", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
