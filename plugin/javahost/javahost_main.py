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
from core.runtime import java, jvm_opts         # noqa: E402
from core.tomcat import registry, installer, service  # noqa: E402
from core.deploy import war, proxy              # noqa: E402
from core.db import pg                          # noqa: E402

INSTANCE_ROOT = "/www/server/javahost/instances"


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
            apps = self._list_apps()
            return panel.ok({
                "java": jdks,
                "tomcat": tomcats,
                "apps": apps,
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

    # ---- apps ----
    def CreateApp(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            major = validate.tomcat_version(panel.attr(get, "version"))
            port = validate.port(panel.attr(get, "port", 8080))
            heap = validate.memory_mb(panel.attr(get, "memory", 512))
            home = installer.home_path(major)
            if not installer.is_installed(major):
                return panel.err("Tomcat %s is not installed" % major)
            java_home = installer.ensure_java(major)
            major_java = java.probe(java_home) or registry.get_line(major).min_java
            base = os.path.join(INSTANCE_ROOT, app)
            self._scaffold_base(base)
            opts, warns = jvm_opts.sanitize(jvm_opts.default_opts(heap), major_java)
            service.write_setenv(base, app, java_home, home, opts, [])
            self._render_instance_conf(base, port)
            service.install_unit(app, java_home, home, base)
            service.enable_start(app)
            return panel.ok({"app": app, "port": port, "warnings": warns})
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

    def DeployWar(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            war_path = panel.attr(get, "war")
            major = validate.tomcat_version(panel.attr(get, "version"))
            if not war_path or not os.path.isfile(war_path):
                return panel.err("WAR not found: %r" % war_path)
            ns = registry.get_line(major).namespace
            warn = war.namespace_warning(war_path, ns)
            base = os.path.join(INSTANCE_ROOT, app)
            target = os.path.join(base, "webapps", "ROOT")
            war.safe_extract(war_path, target)
            return panel.ok({"app": app, "deployed": True, "warning": warn})
        except Exception as e:
            return panel.err(str(e))

    def SetDbEnv(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            base = os.path.join(INSTANCE_ROOT, app)
            mapping = pg.render_env(
                app,
                host=panel.attr(get, "db_host", "127.0.0.1"),
                port=panel.attr(get, "db_port", 5432),
                db=panel.attr(get, "db_name"),
                user=panel.attr(get, "db_user"),
                password=panel.attr(get, "db_password", ""),
            )
            pg.write_env(base, mapping)
            return panel.ok({"app": app, "env": "written (secrets not echoed)"})
        except Exception as e:
            return panel.err(str(e))

    def GetProxyHint(self, get):
        return panel.ok({"include": proxy.include_hint(), "pg": pg.guidance()})

    # ---- helpers ----
    def _list_apps(self):
        out = []
        if os.path.isdir(INSTANCE_ROOT):
            for app in sorted(os.listdir(INSTANCE_ROOT)):
                out.append({"app": app, "status": service.status(app)})
        return out

    def _scaffold_base(self, base):
        from core.util import fs
        for sub in ("conf", "webapps", "logs", "work", "temp", "bin"):
            fs.ensure_dir(os.path.join(base, sub))
        fs.mark_managed(base)

    def _render_instance_conf(self, base, port):
        from core.tomcat import templating
        from core.util import fs
        fs.atomic_write(os.path.join(base, "conf", "server.xml"),
                        templating.render_file("server.xml.tmpl", {"http_port": str(port)}),
                        mode=0o640)
        fs.atomic_write(os.path.join(base, "conf", "context.xml"),
                        templating.render_file("context.xml.tmpl",
                                               {"app": os.path.basename(base)}),
                        mode=0o640)
