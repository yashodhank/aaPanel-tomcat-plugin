# Endpoint reference

Every method the panel can dispatch to JavaHost. The panel imports
`plugin/javahost/javahost_main.py`, instantiates the `javahost_main` class, and
calls `instance.<Method>(get)` where `get` is an attribute namespace of request
params (UI convention: `POST /plugin?action=a&name=javahost&s=<Method>`). Every
method validates its inputs and returns the panel's standard envelope:
`panel.ok(data)` → `{status: True, msg: data}`, `panel.err(msg)` →
`{status: False, msg: msg}`. Secrets (DB passwords) are never echoed back.

This list is the source of truth for the actual methods in `javahost_main.py`.

## Dashboard / status

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetStatus` | — | `{java, tomcat, apps, systemd, supported_tomcat, service_dirs_locked, hardening_hint, exec_filter_active, exec_filter_hint, site_suffix}`. `apps` is `list_apps()` (per-app `type, runtime, tomcat, java, port, context, enabled, backend, domain, ssl, runtime_ok, …`). The single dashboard round-trip. |
| `GetHealthAll` | — | `{health: {app: {up, code, port}}}` — batched health for **all** apps in one call (avoids the per-app `GetHealth` N+1 on each UI poll). |

## Java runtime

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `InstallJava` | `version` | Sync install of Temurin for that major → `{java_home, major}`. |
| `StartInstallJava` | `version` | **Async** install (job kind `install-java`) → `{job_id}`. |
| `StartReinstallJava` | `version`, `to_plugin_dir?` | **Async** reinstall (job `reinstall-java`) → `{job_id}`. |
| `GetJavaUsage` | `version` | `{version, in_use_by: [apps]}` — apps whose pinned `JAVA_HOME` is this major; used to warn before an uninstall. |
| `UninstallJava` | `version`, `force?` | Sync uninstall of a plugin-managed JDK. **Blocked** when apps pin this major unless `force` (returns `{error, in_use_by}`). |
| `StartUninstallJava` | `version`, `force?` | **Async** uninstall (job `uninstall-java`) → `{job_id}`; same in-use block; `force` also stops dependents. |

JavaHost manages only its own JDKs under `runtimes/` (+ distro `/usr/lib/jvm`); it
does not reuse aaPanel's `/usr/local/btjdk`. See
[Java runtime](java-runtime.md).

## Tomcat lifecycle

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `InstallTomcat` | `version` (9 / 10 / 11) | Sync verified install → `{patch, …}`. |
| `UpdateTomcat` | `version` | Upgrade a managed major to the latest patch (atomic, rollback-safe). |
| `UninstallTomcat` | `version` | Sync uninstall. |
| `StartInstallTomcat` | `version` | **Async** install (job `install-tomcat`) → `{job_id}`. |
| `StartUninstallTomcat` | `version` | **Async** uninstall (job `uninstall-tomcat`) → `{job_id}`. |

## Background jobs (Tasks)

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetJobs` | — | `{jobs: [...]}` — all jobs with state (`running`/`done`/`failed`), target, elapsed. Prunes old jobs best-effort. |
| `GetJobLog` | `job_id`, `lines?` (default 200) | One job's live log tail. |

## Apps

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `CreateApp` | `app`, `version`, `port?` (8080), `memory?` (512), `java?` (JDK pin) | Provision a per-app `CATALINA_BASE`; `java` pins the JDK (else the Tomcat line baseline). |
| `CreateJarApp` | `app`, `jar`/`tmp`, `java?` (17), `port?`, `memory?`, `profiles?` | Run an executable / Spring Boot fat-JAR as a service (`SERVER_PORT`, loopback bind). |
| `AppAction` | `app`, `action` | **Sync** start/stop/restart → `{app, status}` (kept for CLI). |
| `StartAppAction` | `app`, `action` (start\|stop\|restart\|repair) | **Async** lifecycle as a detached job → `{job_id, app, action}`. |
| `RepairApp` | `app` | Re-render + reinstall the service/config. |
| `DeleteApp` | `app` | Remove the instance, its files, and the service (marker-gated). |
| `GetAppDetail` | `app` | Per-app detail (`instance.detail`). |
| `GetLogs` | `app`, `lines?` (200, clamped) | Memory-safe per-app log tail. |
| `GetHealth` | `app` | Single-app loopback health probe. |
| `GetMetrics` | `app` | `{pid, cpu_pct, rss_mb, threads, uptime_s}` from `/proc`. **`cpu_pct`** (new in v0.16.2) is sampled over a short interval — not the thread count. |

## Deploy

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `DeployWar` | `app`, `war`, `version` | Zip-slip-safe extract into `webapps/ROOT`; returns a `javax`→`jakarta` namespace `warning` if mismatched. |
| `UploadWar` | `app`, `tmp`/`war`, `version` | Same, for a WAR the panel file API staged to a temp path. |
| `MigrateWar` | `app`, `war`/`tmp`, `version` | Run the Apache `javax`→`jakarta` migration tool, then deploy the converted artifact (Tomcat 10/11). |

## Reverse-proxy sites & SSL

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `SetSite` | `app`, `domain?` | Publish `<app>` reverse-proxied to its loopback port. Domain = `?domain=` or the `<app>.<site_suffix>` convention; with neither, errors (no FQDN guessed). aaPanel site API preferred, nginx-vhost fallback → `{domain, url, via}`. |
| `RemoveSite` | `app` | Remove that site. |
| `SetSiteSSL` | `app`, `enable`, `domain?`, `email?` | `enable` truthy → issue LE cert + switch the vhost to HTTPS (**aaPanel-native ACME first, certbot `--webroot` fallback**); falsy → revert to HTTP (cert kept). Requires a real domain. → `{app, domain, ssl, url, via?}`. |
| `GetSiteStatus` | `app`, `probe_site?` (default on) | On-demand site/cert status: configured domain, cert presence/validity/expiry, HTTP→HTTPS redirect, HTTPS reachability. Powers the drawer's **Site & SSL** block. |
| `GetProxyHint` | — | Nginx include snippet + the DB engines summary (back-compat). |

App connectors bind **127.0.0.1** by design — reachable only via the proxy domain.
See [Reverse proxy & per-site HTTPS](user-guide.md#6-reverse-proxy--per-site-https).

## Databases

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetDbSupport` | — | `{engines: [...]}` — every engine, version range, driver, local detection. |
| `SetDbEnv` | `app`, `db_engine`, `db_host?`, `db_port?`, `db_name`, `db_user`, `db_password`, `db_version?`, `db_ssl?` | Write the secret-safe `app.env`. SSL defaults **off** for loopback hosts, on for remote. Secrets are **not** echoed. |
| `GetDbEnv` | `app` | **Secret-safe** current env → `{configured, engine, url, user, driver, driver_maven, has_password}` — the password is **never** returned, only whether one is set. |

## Hardening / maintenance

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `AllowServices` | — | Register JavaHost in aaPanel System Hardening's process allowlist (append-only, reversible; registers, never bypasses). See [System Hardening](system-hardening.md). |
| `WipePreview` | — | **Dry run** of the Danger-zone wipe: counts + lists per category, removes nothing. `jdks` lists plugin runtimes only. |
| `Wipe` | `confirm` (must equal `WIPE`), `scope` (csv from `apps,jdks,tomcats,sites,full`) | Granular plugin wipe. Stops apps first; **skips in-use** runtimes; never touches the panel cert, other plugins' configs, or any database. |

## Docs (Help viewer)

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetDoc` | `name` (allowlisted: `user-guide`, `system-hardening`, `single-vs-multi-mode`, `databases-java-apps`, `backup-restore`, `troubleshooting`) | Returns a bundled doc's markdown for in-UI rendering (path-traversal-guarded). |

## Dashboard aggregates

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetDashboard` | — | Heavier operational aggregates kept off the fast `GetStatus` poll: `{apps:{total,running,down,runtime_missing}, resources:{cpu_pct_total,rss_mb_total,sampled}, ssl:{with_ssl,expiring_soon,expiring:[...]}, disk:{instances_mb,backups_mb}, recent_tasks:[...]}`. Cert expiry is read from the SSL marker (no openssl). Lazy-loaded by the UI. |

## Backup & restore

Long operations run as async jobs (`{job_id}`); poll `GetJobLog`. See
[Backup, restore & remote storage](backup-restore.md).

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `ListBackups` | `app?` | Newest-first backup records (merges remote when configured); each `{name, app, type, domain, ssl_enabled, created_at, size_mb, location}`. |
| `StartBackup` | `app`, `remote?` | Async — archive the app (excludes logs and LE keys); `remote=1` also uploads. |
| `StartRestore` | `archive` (store name), `as_name?`, `domain?` | Async — restore in place (no `as_name`) or as a new app (reallocated port). Downloads from remote first if needed. |
| `StartRestoreUpload` | `tmp` (staged upload path), `as_name?`, `domain?` | Async — restore an **uploaded** `.tar.gz`; unpacked only via the hardened extractor. |
| `DeleteBackup` | `archive` | Delete a backup (local, and the remote copy if configured). Name strictly validated. |

## Remote object storage (S3-compatible)

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetRemoteStorage` | — | Current config **without the secret key** (`secret_set` flag only): `{provider,endpoint,region,bucket,access_key,prefix,path_style,configured,secret_set}`. |
| `SetRemoteStorage` | `provider`, `endpoint`, `region?`, `bucket`, `access_key`, `secret_key?`, `prefix?`, `path_style?` | Store `0600` `remote.json`. An empty `secret_key` keeps the stored one. |
| `TestRemoteStorage` | — | `{ok, detail}` — HEADs the bucket. |
| `RemoveRemoteStorage` | — | Delete the remote config. |

## Scheduled backups

| Method | Params | Returns / notes |
|--------|--------|-----------------|
| `GetBackupSchedules` | — | `{schedules: [{app, cron, remote, keep}]}`. |
| `SetBackupSchedule` | `app`, `cron` (5-field), `remote?`, `keep?` | Upsert a schedule; regenerates the managed `/etc/cron.d/javahost-backups`. |
| `RemoveBackupSchedule` | `app` | Remove the schedule (clears the cron file when empty). |
