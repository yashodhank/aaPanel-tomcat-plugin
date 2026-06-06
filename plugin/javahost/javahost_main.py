# coding: utf-8
# -----------------------------------------------------------------------------
# JavaHost — Tomcat & Java runtime manager for aaPanel/BaoTa-style panels
# Clean-room, original work. Licensed under Apache-2.0 (see repository LICENSE).
# Contains NO aaPanel source; built against the panel's public plugin API only.
# -----------------------------------------------------------------------------
"""
aaPanel plugin entrypoint. The panel imports this module, instantiates
`javahost_main`, and calls `instance.<Method>(get)` where `get` is an attribute
namespace of request params. Every method validates input and returns the
panel's standard {status, msg} envelope via core.compat.aapanel.

All real logic lives in `core/`; this file is thin glue so it stays auditable.
"""
from __future__ import annotations

import os
import sys

# Make `core` importable regardless of how the panel loads the plugin.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core.compat import aapanel as panel       # noqa: E402
from core.util import validate                  # noqa: E402
from core.runtime import java                   # noqa: E402
from core.tomcat import registry, installer, service, instance  # noqa: E402
from core.deploy import war, proxy              # noqa: E402
from core.db import engines as dbengines        # noqa: E402


class javahost_main(object):
    # ---- dashboard ----
    def GetStatus(self, get=None):
        try:
            jdks = {str(k): v for k, v in java.detect().items()}
            tomcats = {}
            for major in registry.LINES:
                ver = installer.is_installed(major)
                if ver:
                    tomcats[major] = {
                        "patch": ver,
                        "min_java": registry.get_line(major).min_java,
                        "namespace": registry.get_line(major).namespace,
                    }
            return panel.ok({
                "java": jdks,
                "tomcat": tomcats,
                "apps": instance.list_apps(),
                "systemd": service.have_systemd(),
                "supported_tomcat": sorted(registry.LINES),
            })
        except Exception as e:
            return panel.err(str(e))

    # ---- java runtime ----
    def InstallJava(self, get):
        try:
            major = validate.java_major(panel.attr(get, "version"))
            home = java.install_temurin(major)
            panel.log("InstallJava", "jdk %d -> %s" % (major, home))
            return panel.ok({"java_home": home, "major": major})
        except Exception as e:
            return panel.err(str(e))

    # ---- tomcat lifecycle ----
    def InstallTomcat(self, get):
        try:
            major = validate.tomcat_version(panel.attr(get, "version"))
            res = installer.install(major)
            panel.log("InstallTomcat", "tomcat %s (%s)" % (major, res["patch"]))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def UninstallTomcat(self, get):
        try:
            major = validate.tomcat_version(panel.attr(get, "version"))
            installer.uninstall(major)
            panel.log("UninstallTomcat", "tomcat %s" % major)
            return panel.ok("uninstalled")
        except Exception as e:
            return panel.err(str(e))

    def UpdateTomcat(self, get):
        """Upgrade a managed Tomcat major to the latest patch (atomic, rollback-safe)."""
        try:
            major = validate.tomcat_version(panel.attr(get, "version"))
            res = installer.install(major)  # resolves latest patch; staged + verified
            panel.log("UpdateTomcat", "tomcat %s -> %s" % (major, res["patch"]))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    # ---- apps ----
    def CreateApp(self, get):
        try:
            res = instance.create(
                app=panel.attr(get, "app"),
                major=panel.attr(get, "version"),
                port=panel.attr(get, "port", 8080),
                memory_mb=panel.attr(get, "memory", 512),
            )
            panel.log("CreateApp", "%(app)s tomcat=%(tomcat)s port=%(port)s" % res)
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def AppAction(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            what = panel.attr(get, "action")
            service.action(app, what)
            return panel.ok({"app": app, "status": service.status(app)})
        except Exception as e:
            return panel.err(str(e))

    def DeleteApp(self, get):
        try:
            res = instance.delete(panel.attr(get, "app"))
            panel.log("DeleteApp", res["app"])
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def RepairApp(self, get):
        try:
            return panel.ok(instance.repair(panel.attr(get, "app")))
        except Exception as e:
            return panel.err(str(e))

    def GetAppDetail(self, get):
        try:
            return panel.ok(instance.detail(panel.attr(get, "app")))
        except Exception as e:
            return panel.err(str(e))

    def GetLogs(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            lines = panel.attr(get, "lines", 200)
            return panel.ok({"app": app, "log": instance.tail_log(app, int(lines))})
        except Exception as e:
            return panel.err(str(e))

    def DeployWar(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            war_path = panel.attr(get, "war")
            major = validate.tomcat_version(panel.attr(get, "version"))
            if not war_path or not os.path.isfile(war_path):
                return panel.err("WAR not found: %r" % war_path)
            ns = registry.get_line(major).namespace
            warn = war.namespace_warning(war_path, ns)
            target = os.path.join(instance.base_path(app), "webapps", "ROOT")
            war.safe_extract(war_path, target)
            return panel.ok({"app": app, "deployed": True, "warning": warn})
        except Exception as e:
            return panel.err(str(e))

    def UploadWar(self, get):
        """Deploy a WAR the panel has staged to a temp path (`tmp`). The UI uploads
        the file via the panel's file API; this wires that staged path into the
        zip-slip-safe deploy flow."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            tmp = panel.attr(get, "tmp") or panel.attr(get, "war")
            major = validate.tomcat_version(panel.attr(get, "version"))
            if not tmp or not os.path.isfile(tmp):
                return panel.err("uploaded WAR not found at staged path: %r" % tmp)
            warn = war.namespace_warning(tmp, registry.get_line(major).namespace)
            target = os.path.join(instance.base_path(app), "webapps", "ROOT")
            war.safe_extract(tmp, target)
            panel.log("UploadWar", "%s <- %s" % (app, os.path.basename(str(tmp))))
            return panel.ok({"app": app, "deployed": True, "warning": warn})
        except Exception as e:
            return panel.err(str(e))

    def MigrateWar(self, get):
        """Convert a javax.* WAR to jakarta.* (Apache migration tool), then deploy
        the migrated artifact to the app's webapps/ROOT for Tomcat 10/11."""
        try:
            from core.util import fs
            app = validate.identifier(panel.attr(get, "app"), "app")
            major = validate.tomcat_version(panel.attr(get, "version"))
            src = panel.attr(get, "war") or panel.attr(get, "tmp")
            if not src or not os.path.isfile(src):
                return panel.err("source WAR not found: %r" % src)
            java_home = installer.ensure_java(major)
            tmp = fs.mkdtemp("javahost-migrate-")
            out = os.path.join(tmp, "migrated.war")
            war.migrate(src, out, java_home)
            target = os.path.join(instance.base_path(app), "webapps", "ROOT")
            war.safe_extract(out, target)
            fs.safe_rmtree(tmp, require_marker=False) if tmp.startswith("/tmp") else None
            panel.log("MigrateWar", "%s migrated+deployed" % app)
            return panel.ok({"app": app, "migrated": True, "deployed": True})
        except Exception as e:
            return panel.err(str(e))

    def SetDbEnv(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            base = instance.base_path(app)
            engine = dbengines.get(panel.attr(get, "db_engine", "postgresql"))
            mapping = engine.render_env(
                host=panel.attr(get, "db_host", "127.0.0.1"),
                port=panel.attr(get, "db_port", None),  # defaults to engine port
                db=panel.attr(get, "db_name"),
                user=panel.attr(get, "db_user"),
                password=panel.attr(get, "db_password", ""),
                version=panel.attr(get, "db_version"),  # optional; any supported version
            )
            dbengines.write_app_env(base, mapping)
            return panel.ok({"app": app, "engine": engine.name,
                             "env": "written (secrets not echoed)",
                             "driver": mapping["DB_DRIVER_MAVEN"]})
        except Exception as e:
            return panel.err(str(e))

    def GetDbSupport(self, get=None):
        """All supported DB engines, version ranges, drivers, and local detection."""
        try:
            return panel.ok({"engines": dbengines.support_matrix()})
        except Exception as e:
            return panel.err(str(e))

    def GetProxyHint(self, get=None):
        eng = dbengines.get("postgresql")
        return panel.ok({"include": proxy.include_hint(), "db": eng.guidance()})
