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
from core.deploy import war, proxy, ssl, sitestatus  # noqa: E402
from core.db import engines as dbengines        # noqa: E402
from core import jobs                            # noqa: E402
from core import config                          # noqa: E402
from core import maintenance                      # noqa: E402
from core import dashboard                        # noqa: E402
from core.backup import store as backupstore      # noqa: E402
from core.backup import remote as backupremote    # noqa: E402
from core.backup import schedule as backupschedule  # noqa: E402


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
            # service_dirs_locked is true ONLY when the plugin genuinely cannot
            # manage services — i.e. neither backend is writable nor safely
            # lift-and-relock manageable (manage_hardening off / chattr absent).
            can_sysd = service.have_systemd() and service.can_manage(service.SYSTEMD_DIR)
            can_initd = service.can_manage(service.INITD_DIR)
            hardening_locked = not (can_sysd or can_initd)
            # Detect the global LD_PRELOAD execve filter (bt_security/usranalyse)
            # that blocks new daemons — separate from the immutable-dir layer.
            try:
                from core.compat import syssafe
                exec_filter = syssafe.exec_filter()
            except Exception:
                exec_filter = {"active": False, "guidance": ""}
            return panel.ok({
                "java": jdks,
                "tomcat": tomcats,
                "apps": instance.list_apps(),
                "systemd": service.have_systemd(),
                "supported_tomcat": sorted(registry.LINES),
                "service_dirs_locked": hardening_locked,
                "hardening_hint": (
                    "Service directories are immutable (likely aaPanel 'System "
                    "Hardening'). Disable it (or lift chattr +i on /etc/systemd/system) "
                    "so JavaHost can register Tomcat/JAR services." if hardening_locked else ""),
                "exec_filter_active": exec_filter.get("active", False),
                "exec_filter_hint": exec_filter.get("guidance", ""),
                # Public-domain suffix (config "site_suffix", "" when unset) so the
                # UI can offer "<app>.<suffix>" defaults; empty => UI must prompt.
                "site_suffix": config.site_suffix(),
            })
        except Exception as e:
            return panel.err(str(e))

    def GetDashboard(self, get=None):
        """Heavier operational aggregates (per-app CPU/RSS, SSL expiry, disk,
        recent tasks) for the Dashboard. Kept separate from GetStatus so the
        fast status poll stays cheap; the UI lazy-loads this on tab activation."""
        try:
            return panel.ok(dashboard.summary())
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

    def GetJavaUsage(self, get):
        """Apps whose pinned JAVA_HOME is this major — so the UI can warn before
        an uninstall. -> {"version": N, "in_use_by": [apps]}."""
        try:
            major = validate.java_major(panel.attr(get, "version"))
            return panel.ok({"version": major, "in_use_by": java.usage(major)})
        except Exception as e:
            return panel.err(str(e))

    def UninstallJava(self, get):
        """Remove a plugin-managed JDK (sync). Refuses the panel-owned JDK; blocks
        when apps pin this major unless force is set, returning the in_use_by
        list so the UI can prompt."""
        try:
            major = validate.java_major(panel.attr(get, "version"))
            force = str(panel.attr(get, "force", "")).lower() in ("1", "true", "yes", "on")
            in_use = java.usage(major)
            if in_use and not force:
                return panel.err({"error": "Java %d is in use" % major,
                                  "in_use_by": in_use})
            res = java.uninstall(major, force=force)
            panel.log("UninstallJava", "jdk %d removed=%s" % (major, res.get("removed")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def StartUninstallJava(self, get):
        """Async JDK uninstall (job kind 'uninstall-java'). Blocks on dependents
        (returns in_use_by) unless force; otherwise the job records the result."""
        try:
            major = validate.java_major(panel.attr(get, "version"))
            force = str(panel.attr(get, "force", "")).lower() in ("1", "true", "yes", "on")
            in_use = java.usage(major)
            if in_use and not force:
                return panel.err({"error": "Java %d is in use" % major,
                                  "in_use_by": in_use})
            argv = jobs.python_work(
                "from core.runtime import java\n"
                "java.uninstall(%d, force=%r)\n" % (major, force))
            job_id = jobs.start("uninstall-java", major, argv)
            panel.log("StartUninstallJava", "jdk %d -> job %s" % (major, job_id))
            return panel.ok({"job_id": job_id})
        except Exception as e:
            return panel.err(str(e))

    def StartReinstallJava(self, get):
        """Async JDK reinstall (job kind 'reinstall-java'). to_plugin_dir keeps a
        panel-owned JDK untouched and installs the plugin's own copy under the
        runtimes dir; otherwise uninstall(force=True) then install_temurin."""
        try:
            major = validate.java_major(panel.attr(get, "version"))
            to_plugin = str(panel.attr(get, "to_plugin_dir", "")).lower() \
                in ("1", "true", "yes", "on")
            argv = jobs.python_work(
                "from core.runtime import java\n"
                "java.reinstall(%d, to_plugin_dir=%r)\n" % (major, to_plugin))
            job_id = jobs.start("reinstall-java", major, argv)
            panel.log("StartReinstallJava",
                      "jdk %d to_plugin=%s -> job %s" % (major, to_plugin, job_id))
            return panel.ok({"job_id": job_id})
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

    # ---- async background jobs ----------------------------------------------
    # The download+extract in InstallJava/InstallTomcat is too long for a single
    # panel AJAX request (it times out and the UI flashes a false error). These
    # endpoints return a job_id at once and run the work in a detached child the
    # UI polls via GetJobs / GetJobLog. The synchronous endpoints above stay for
    # CLI / back-compat.
    def StartInstallJava(self, get):
        try:
            major = validate.java_major(panel.attr(get, "version"))
            argv = jobs.python_work(
                "from core.runtime import java\n"
                "java.install_temurin(%d)\n" % major)
            job_id = jobs.start("install-java", major, argv)
            panel.log("StartInstallJava", "jdk %d -> job %s" % (major, job_id))
            return panel.ok({"job_id": job_id})
        except Exception as e:
            return panel.err(str(e))

    def StartInstallTomcat(self, get):
        try:
            major = validate.tomcat_version(panel.attr(get, "version"))
            argv = jobs.python_work(
                "from core.tomcat import installer\n"
                "installer.install(%r)\n" % major)
            job_id = jobs.start("install-tomcat", major, argv)
            panel.log("StartInstallTomcat", "tomcat %s -> job %s" % (major, job_id))
            return panel.ok({"job_id": job_id})
        except Exception as e:
            return panel.err(str(e))

    def StartUninstallTomcat(self, get):
        try:
            major = validate.tomcat_version(panel.attr(get, "version"))
            argv = jobs.python_work(
                "from core.tomcat import installer\n"
                "installer.uninstall(%r)\n" % major)
            job_id = jobs.start("uninstall-tomcat", major, argv)
            panel.log("StartUninstallTomcat", "tomcat %s -> job %s" % (major, job_id))
            return panel.ok({"job_id": job_id})
        except Exception as e:
            return panel.err(str(e))

    def GetJobs(self, get=None):
        try:
            # Best-effort GC so JOBS_ROOT can't grow unbounded across installs.
            try:
                jobs.prune()
            except Exception:
                pass
            return panel.ok({"jobs": jobs.list_jobs(),
                             "skipped": jobs.count_skipped()})
        except Exception as e:
            return panel.err(str(e))

    def GetJobLog(self, get):
        try:
            job_id = panel.attr(get, "job_id")
            lines = int(panel.attr(get, "lines", 200))
            return panel.ok(jobs.read_log(job_id, lines))
        except Exception as e:
            return panel.err(str(e))

    # ---- reverse-proxy sites ------------------------------------------------
    def SetSite(self, get):
        """Publish <app> at <domain> reverse-proxied to its loopback port. The
        domain comes from ?domain= or, if a site_suffix is configured, the
        convention "<app>.<suffix>"; with neither, the caller MUST pass ?domain=
        (no FQDN is ever guessed). Tries aaPanel's site API, falls back to our
        nginx vhost. Returns {domain, url}."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            domain = panel.attr(get, "domain") or proxy.default_domain(app)
            if not domain:
                return panel.err("no domain: pass ?domain= or set site_suffix in config")
            domain = validate.domain(domain)
            port = instance.detail(app).get("port") or instance.health(app).get("port")
            if not port:
                return panel.err("cannot resolve port for app %r (is it created?)" % app)
            res = proxy.set_site(app, domain, int(port))
            panel.log("SetSite", "%s -> %s (%s)" % (app, res["domain"], res.get("via")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def RemoveSite(self, get):
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            res = proxy.remove_site(app)
            panel.log("RemoveSite", app)
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def SetSiteSSL(self, get):
        """Provision (or revoke) Let's Encrypt SSL for <app>'s reverse-proxy site.

        `enable` truthy -> issue + switch the vhost to HTTPS (aaPanel native ACME
        first, certbot fallback); falsy -> revert to plain HTTP (cert kept).
        Returns {app, domain, ssl, url, via?}."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            # Require a REAL domain: stored site domain, explicit ?domain=, or a
            # convention domain (only when site_suffix is configured). Never issue
            # a cert against a guessed FQDN.
            domain = (proxy.read_domain(app)
                      or panel.attr(get, "domain", None)
                      or proxy.default_domain(app))
            if not domain:
                return panel.err("no domain configured for %r — create a reverse-proxy "
                                 "site first or pass domain" % app)
            domain = validate.domain(domain)
            port = instance.detail(app).get("port") or instance.health(app).get("port")
            if not port:
                return panel.err("cannot resolve port for app %r (is it created?)" % app)
            enable_raw = panel.attr(get, "enable")
            want = str(enable_raw).lower() not in ("0", "false", "no", "off", "", "none") \
                if enable_raw is not None else False
            if want:
                email = panel.attr(get, "email", None) or None
                res = ssl.enable(app, domain, int(port), email=email)
            else:
                res = ssl.disable(app, domain, int(port))
            res = dict(res)
            res.setdefault("app", app)
            res.setdefault("domain", domain)
            panel.log("SetSiteSSL", "%s ssl=%s via=%s" % (app, res.get("ssl"), res.get("via")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    # ---- apps ----
    def CreateApp(self, get):
        try:
            jv = panel.attr(get, "java", None)
            prefer_java = int(jv) if jv is not None and str(jv) not in ("", "None", "0") else None
            res = instance.create(
                app=panel.attr(get, "app"),
                major=panel.attr(get, "version"),
                port=panel.attr(get, "port", 8080),
                memory_mb=panel.attr(get, "memory", 512),
                prefer_java=prefer_java,
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

    def StartAppAction(self, get):
        """Async app lifecycle: run start|stop|restart|repair in a detached job so
        a slow systemd transition can't time out the panel AJAX worker. Returns
        {job_id, app, action} at once; the UI polls GetJobs/GetJobLog and reads
        the resulting status the job prints. The sync AppAction stays for CLI."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            action = str(panel.attr(get, "action") or "").strip().lower()
            if action not in ("start", "stop", "restart", "repair"):
                return panel.err("invalid action: %r (start|stop|restart|repair)" % action)
            if action == "repair":
                body = ("from core.tomcat import instance, service\n"
                        "instance.repair(%r)\n"
                        "print('status:', service.status(%r))\n" % (app, app))
            else:
                body = ("from core.tomcat import service\n"
                        "service.action(%r, %r)\n"
                        "print('status:', service.status(%r))\n" % (app, action, app))
            argv = jobs.python_work(body)
            job_id = jobs.start("app-" + action, app, argv)
            panel.log("StartAppAction", "%s %s -> job %s" % (app, action, job_id))
            return panel.ok({"job_id": job_id, "app": app, "action": action})
        except Exception as e:
            return panel.err(str(e))

    def GetSiteStatus(self, get):
        """On-demand SSL/site status for an app's reverse-proxy site (cert expiry
        + http/https reachability). Heavier than the list view, so it's a separate
        endpoint the detail drawer calls. Set probe_site=0 to skip network probes."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            probe_raw = panel.attr(get, "probe_site", True)
            probe_site = str(probe_raw).lower() not in ("0", "false", "no", "off", "")
            return panel.ok(sitestatus.probe(app, probe_site=probe_site))
        except Exception as e:
            return panel.err(str(e))

    def DeleteApp(self, get):
        try:
            res = instance.delete(panel.attr(get, "app"))
            panel.log("DeleteApp", res["app"])
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    # ---- backup / restore ----
    def ListBackups(self, get=None):
        try:
            app = panel.attr(get, "app", None) if get is not None else None
            app = validate.identifier(app, "app") if app else None
            return panel.ok({"backups": backupstore.list_backups(app=app, include_remote=True)})
        except Exception as e:
            return panel.err(str(e))

    def StartBackup(self, get):
        """Archive an app as a detached job (tar of a webapp can be slow). Returns
        {job_id}. `remotes` selects storage destinations: a csv of profile ids, or
        "all" for every enabled profile; legacy remote=1 == all."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            remotes = panel.attr(get, "remotes", "") or ""
            if not remotes and str(panel.attr(get, "remote", "")).lower() in ("1", "true", "yes", "on"):
                remotes = "all"
            body = ("from core.backup import store\n"
                    "r = store.backup_app(%r, remotes=%r)\n"
                    "print('backup:', r['name'], r['size_mb'], 'MB', 'uploaded_to=' + str(r.get('uploaded_to')))\n"
                    "[print('upload FAILED', k, v.get('detail')) for k, v in (r.get('upload_results') or {}).items() if not v.get('ok')]\n"
                    % (app, remotes))
            job_id = jobs.start("backup", app, jobs.python_work(body))
            panel.log("StartBackup", "%s remotes=%s -> job %s" % (app, remotes or "(local)", job_id))
            return panel.ok({"job_id": job_id, "app": app})
        except Exception as e:
            return panel.err(str(e))

    def StartRestore(self, get):
        """Restore an app from a backup archive as a detached job. `archive` is a
        backup name in the store; `as_name` (optional) restores as a NEW app on a
        reallocated port; `domain` (optional) remaps the site. Returns {job_id}."""
        try:
            name = panel.attr(get, "archive")
            backupstore._backup_path(name)  # validate name + containment (raises on bad)
            as_raw = panel.attr(get, "as_name", None)
            as_name = validate.identifier(as_raw, "as_name") if as_raw else None
            dom = panel.attr(get, "domain", None) or None
            prof_raw = panel.attr(get, "profile", None)
            profile = validate.identifier(prof_raw, "profile") if prof_raw else None
            # ensure_local pulls the archive from the named storage profile (or any
            # that has it) when it isn't present on disk — remote-only restores work.
            body = ("from core.backup import store\n"
                    "p = store.ensure_local(%r, profile=%r)\n"
                    "r = store.restore(p, as_name=%r, domain=%r)\n"
                    "print('restore:', r['app'], r['mode'], 'port=' + str(r.get('port')),"
                    " 'status=' + str(r.get('status')))\n"
                    "print('ssl_warning:', r['ssl_warning']) if r.get('ssl_warning') else None\n"
                    % (name, profile, as_name, dom))
            job_id = jobs.start("restore", as_name or name, jobs.python_work(body))
            panel.log("StartRestore", "%s as=%s -> job %s" % (name, as_name or "(overwrite)", job_id))
            return panel.ok({"job_id": job_id, "archive": name, "as_name": as_name})
        except Exception as e:
            return panel.err(str(e))

    def StartRestoreUpload(self, get):
        """Restore from an UPLOADED archive the panel staged to a temp path (`tmp`).
        This is the untrusted-input path — the archive is unpacked only through the
        hardened safe_extract_tar (symlink/traversal/device rejection)."""
        try:
            tmp = panel.attr(get, "tmp") or panel.attr(get, "archive")
            rp = os.path.realpath(str(tmp or ""))
            ok = (os.path.isfile(rp) and rp.endswith(".tar.gz")
                  and (rp.startswith("/tmp/") or rp.startswith(os.path.realpath(maintenance.DATA_ROOT) + os.sep)))
            if not ok:
                return panel.err("uploaded archive not found at a valid staged path: %r" % tmp)
            as_raw = panel.attr(get, "as_name", None)
            as_name = validate.identifier(as_raw, "as_name") if as_raw else None
            dom = panel.attr(get, "domain", None) or None
            body = ("from core.backup import store\n"
                    "r = store.restore(%r, as_name=%r, domain=%r)\n"
                    "print('restore:', r['app'], r['mode'], 'port=' + str(r.get('port')))\n"
                    "print('ssl_warning:', r['ssl_warning']) if r.get('ssl_warning') else None\n"
                    % (rp, as_name, dom))
            job_id = jobs.start("restore", as_name or os.path.basename(rp), jobs.python_work(body))
            panel.log("StartRestoreUpload", "%s as=%s -> job %s" % (os.path.basename(rp), as_name or "(overwrite)", job_id))
            return panel.ok({"job_id": job_id, "as_name": as_name})
        except Exception as e:
            return panel.err(str(e))

    def DeleteBackup(self, get):
        try:
            locations = panel.attr(get, "locations", None) or None
            res = backupstore.delete_backup(panel.attr(get, "archive"), locations=locations)
            panel.log("DeleteBackup", "%s locations=%s" % (res["name"], locations or "all"))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    # ---- remote object storage (S3 / Wasabi / MinIO / B2 / R2) ----
    def GetRemoteStorage(self, get=None):
        """Current remote-storage config. Secret key is NEVER returned (only a
        `secret_set` flag), mirroring the GetDbEnv secret-safe pattern."""
        try:
            return panel.ok(backupremote.get_config(redacted=True))
        except Exception as e:
            return panel.err(str(e))

    def SetRemoteStorage(self, get):
        try:
            path_raw = panel.attr(get, "path_style", "1")
            res = backupremote.set_config(
                provider=panel.attr(get, "provider", "other"),
                endpoint=panel.attr(get, "endpoint", ""),
                region=panel.attr(get, "region", "us-east-1"),
                bucket=panel.attr(get, "bucket", ""),
                access_key=panel.attr(get, "access_key", ""),
                secret_key=panel.attr(get, "secret_key", ""),
                prefix=panel.attr(get, "prefix", ""),
                path_style=str(path_raw).lower() not in ("0", "false", "no", "off"),
            )
            panel.log("SetRemoteStorage", "%s %s/%s" % (res.get("provider"), res.get("endpoint"), res.get("bucket")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def TestRemoteStorage(self, get=None):
        try:
            return panel.ok(backupremote.test())
        except Exception as e:
            return panel.err(str(e))

    def RemoveRemoteStorage(self, get=None):
        try:
            res = backupremote.remove()
            panel.log("RemoveRemoteStorage", "removed=%s" % res.get("removed"))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    # ---- storage profiles (multi-destination) ----
    def ListRemoteProfiles(self, get=None):
        """All storage destinations, secret-safe (secret keys never returned)."""
        try:
            return panel.ok({"profiles": backupremote.list_profiles(redacted=True)})
        except Exception as e:
            return panel.err(str(e))

    def _profile_fields(self, get):
        path_raw = panel.attr(get, "path_style", "1")
        enabled_raw = panel.attr(get, "enabled", "1")
        # NOTE: the display name travels as `label`, never `name` — aaPanel's request
        # router treats a POST `name` as the plugin/module name and rejects values
        # with spaces/symbols ("module_name ... cannot contain special symbols").
        return dict(
            name=panel.attr(get, "label", ""),
            provider=panel.attr(get, "provider", "other"),
            endpoint=panel.attr(get, "endpoint", ""),
            region=panel.attr(get, "region", "us-east-1"),
            bucket=panel.attr(get, "bucket", ""),
            access_key=panel.attr(get, "access_key", ""),
            secret_key=panel.attr(get, "secret_key", ""),
            prefix=panel.attr(get, "prefix", ""),
            path_style=str(path_raw).lower() not in ("0", "false", "no", "off"),
            enabled=str(enabled_raw).lower() not in ("0", "false", "no", "off"),
        )

    def AddRemoteProfile(self, get):
        try:
            f = self._profile_fields(get)
            res = backupremote.add_profile(
                name=f["name"], provider=f["provider"], endpoint=f["endpoint"],
                region=f["region"], bucket=f["bucket"], access_key=f["access_key"],
                secret_key=f["secret_key"], prefix=f["prefix"],
                path_style=f["path_style"], pid=panel.attr(get, "id", ""),
                enabled=f["enabled"])
            panel.log("AddRemoteProfile", "%s %s/%s" % (res.get("id"), res.get("endpoint"), res.get("bucket")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def UpdateRemoteProfile(self, get):
        try:
            pid = validate.identifier(panel.attr(get, "id"), "profile id")
            f = self._profile_fields(get)
            res = backupremote.update_profile(pid, **f)
            panel.log("UpdateRemoteProfile", pid)
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def DeleteRemoteProfile(self, get):
        try:
            pid = validate.identifier(panel.attr(get, "id"), "profile id")
            force = str(panel.attr(get, "force", "")).lower() in ("1", "true", "yes", "on")
            res = backupremote.delete_profile(pid, force=force)
            panel.log("DeleteRemoteProfile", "%s removed=%s" % (pid, res.get("removed")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def TestRemoteProfile(self, get):
        try:
            pid = validate.identifier(panel.attr(get, "id"), "profile id")
            return panel.ok(backupremote.test_profile(pid))
        except Exception as e:
            return panel.err(str(e))

    # ---- scheduled backups ----
    def GetBackupSchedules(self, get=None):
        try:
            return panel.ok({"schedules": backupschedule.list_schedules()})
        except Exception as e:
            return panel.err(str(e))

    def SetBackupSchedule(self, get):
        try:
            remotes = panel.attr(get, "remotes", "") or ""
            if not remotes and str(panel.attr(get, "remote", "")).lower() in ("1", "true", "yes", "on"):
                remotes = "all"
            res = backupschedule.set_schedule(
                app=validate.identifier(panel.attr(get, "app"), "app"),
                cron_expr=panel.attr(get, "cron"),
                remotes=remotes,
                keep=int(panel.attr(get, "keep", 7)),
            )
            panel.log("SetBackupSchedule", "%s cron=%s keep=%s" % (res["app"], res["cron"], res["keep"]))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def RemoveBackupSchedule(self, get):
        try:
            res = backupschedule.remove_schedule(validate.identifier(panel.attr(get, "app"), "app"))
            panel.log("RemoveBackupSchedule", res["app"])
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

    def GetHealth(self, get):
        try:
            return panel.ok(instance.health(panel.attr(get, "app")))
        except Exception as e:
            return panel.err(str(e))

    def GetDbEnv(self, get):
        """Current DB connection env for <app>, SECRET-SAFE (never returns the
        password — only whether one is set). Lets the UI show what's configured."""
        try:
            app = validate.identifier(panel.attr(get, "app"), "app")
            env = instance._read_app_env(instance.base_path(app))
            url = env.get("DB_URL", "") or ""
            engine = None
            if url.startswith("jdbc:"):
                parts = url.split(":", 2)
                engine = parts[1] if len(parts) > 1 else None
            elif url.startswith("mongodb"):
                engine = "mongodb"
            return panel.ok({
                "app": app,
                "configured": bool(url),
                "engine": engine,
                "url": url or None,            # host/port/db only — never the password
                "user": env.get("DB_USER") or None,
                "driver": env.get("DB_DRIVER") or None,
                "driver_maven": env.get("DB_DRIVER_MAVEN") or None,
                "has_password": bool(env.get("DB_PASSWORD")),
            })
        except Exception as e:
            return panel.err(str(e))

    def GetHealthAll(self, get=None):
        """Batched health for all apps in one round-trip (avoids the per-app
        GetHealth N+1 on each UI poll). -> {"health": {app: {up, code, port}}}."""
        try:
            return panel.ok({"health": instance.health_all()})
        except Exception as e:
            return panel.err(str(e))

    def GetMetrics(self, get):
        """Lightweight JVM/process metrics (pid, RSS MB, threads, uptime) from /proc."""
        try:
            return panel.ok(instance.metrics(panel.attr(get, "app")))
        except Exception as e:
            return panel.err(str(e))

    def AllowServices(self, get=None):
        """One-click: register JavaHost in aaPanel System Hardening's process
        allowlist (append-only, reversible). Registers — never bypasses."""
        try:
            from core.compat import syssafe
            res = syssafe.whitelist_javahost()
            panel.log("AllowServices", "added=%s" % res.get("added"))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def CreateJarApp(self, get):
        """Run an executable / Spring Boot fat-JAR (staged at `jar`) as a service."""
        try:
            res = instance.create_jar(
                app=panel.attr(get, "app"),
                jar_src=panel.attr(get, "jar") or panel.attr(get, "tmp"),
                java_major=panel.attr(get, "java", 17),
                port=panel.attr(get, "port", None),
                memory_mb=panel.attr(get, "memory", 512),
                profiles=panel.attr(get, "profiles", ""),
            )
            panel.log("CreateJarApp", "%(app)s jar port=%(port)s springboot=%(springboot)s" % res)
            return panel.ok(res)
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
            host = panel.attr(get, "db_host", "127.0.0.1")
            # SSL: honour explicit db_ssl; otherwise default OFF for loopback hosts
            # (local DBs usually have no TLS) and ON for remote hosts.
            ssl_raw = panel.attr(get, "db_ssl", None)
            if ssl_raw is None:
                ssl = str(host) not in ("127.0.0.1", "localhost", "::1")
            else:
                ssl = str(ssl_raw).lower() not in ("0", "false", "no", "off", "")
            mapping = engine.render_env(
                host=host,
                port=panel.attr(get, "db_port", None),  # defaults to engine port
                db=panel.attr(get, "db_name"),
                user=panel.attr(get, "db_user"),
                password=panel.attr(get, "db_password", ""),
                version=panel.attr(get, "db_version"),  # optional; any supported version
                ssl=ssl,
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

    # docs bundled with the plugin, served on-the-fly to the Help viewer
    _DOCS_DIR = os.path.join(_HERE, "docs")
    _ALLOWED_DOCS = ("user-guide", "system-hardening", "single-vs-multi-mode",
                     "databases-java-apps", "backup-restore", "troubleshooting")

    def GetDoc(self, get):
        """Return a bundled doc's markdown for in-UI rendering (no 404 file links)."""
        try:
            name = panel.attr(get, "name", "")
            if name not in self._ALLOWED_DOCS:
                return panel.err("unknown document: %r" % name)
            path = os.path.realpath(os.path.join(self._DOCS_DIR, name + ".md"))
            root = os.path.realpath(self._DOCS_DIR)
            if not (path == root or path.startswith(root + os.sep)) or not os.path.isfile(path):
                return panel.err("document not found: %s" % name)
            with open(path, encoding="utf-8", errors="replace") as f:
                return panel.ok({"name": name, "content": f.read()})
        except Exception as e:
            return panel.err(str(e))

    # ---- Danger-zone maintenance (granular wipe) ----------------------------
    def WipePreview(self, get=None):
        """Dry-run: counts + lists of what each wipe category would remove,
        without removing anything. jdks lists plugin runtimes only (never the
        panel btjdk)."""
        try:
            return panel.ok(maintenance.wipe_preview())
        except Exception as e:
            return panel.err(str(e))

    def Wipe(self, get):
        """Granular plugin wipe. Requires confirm='WIPE' and a scope csv drawn
        from {apps,jdks,tomcats,sites,full}. Never touches the panel JDK/cert,
        other plugins' configs, or any database."""
        try:
            confirm = panel.attr(get, "confirm", "")
            if confirm != maintenance.CONFIRM:
                return panel.err("confirmation required: pass confirm=%r"
                                 % maintenance.CONFIRM)
            scope = panel.attr(get, "scope", "")
            res = maintenance.wipe(scope, confirm)
            panel.log("Wipe", "scope=%s performed=%s" % (scope, res.get("performed")))
            return panel.ok(res)
        except Exception as e:
            return panel.err(str(e))

    def GetProxyHint(self, get=None):
        try:
            dbs = []
            for name in ("postgresql", "mysql", "mariadb", "mongodb"):
                e = dbengines.get(name)
                dbs.append({"engine": e.name, "label": e.label, "default_port": e.default_port,
                            "versions": "%s–%s" % (e.versions[0], e.versions[-1]),
                            "driver": e.recommend_driver(), "guidance": e.guidance()})
            return panel.ok({"include": proxy.include_hint(),
                             "databases": dbs,
                             "db": dbengines.get("postgresql").guidance()})  # back-compat
        except Exception as e:
            return panel.err(str(e))
