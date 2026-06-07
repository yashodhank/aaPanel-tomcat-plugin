# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

## [0.20.0] — 2026-06-07

### Added (multiple storage destinations + dedicated Backups tab)
- **Multiple S3 storage destinations.** Remote storage is now a **registry of named
  profiles** (`remotes.json`, `0600`) instead of a single bucket — add/update/delete/
  test as many as you like (Wasabi · MinIO · Backblaze B2 · Cloudflare R2 · AWS).
  Endpoints: `ListRemoteProfiles`, `AddRemoteProfile`, `UpdateRemoteProfile`,
  `DeleteRemoteProfile`, `TestRemoteProfile`. Secret keys are never returned (only a
  `secret_set` flag). A legacy single-config `remote.json` auto-migrates to a
  `default` profile. Deleting a profile a schedule uses warns and detaches on confirm.
- **Select multiple destinations** per backup and per schedule. `StartBackup{remotes}`
  and `SetBackupSchedule{remotes}` take a csv of profile ids (or `all`); a backup is
  fanned out to each, with **per-destination success/failure** reported (a partial
  failure keeps the local copy).
- **Backup state / locations.** `ListBackups` now tags each backup with `locations`
  (the union of `local` + every destination that holds it); restore is
  source-profile-aware (`StartRestore{profile}` / remote-only download). `DeleteBackup`
  takes a `locations` selector; retention prunes each destination independently.
- **Dedicated Backups tab.** A new top-level **Backups** tab consolidates Storage
  destinations, Backups (with location badges, restore, delete, restore-from-file,
  back-up-now with a destination multiselect), and Schedules — moved out of
  Applications/Settings. Per-app **Back up** / **Schedule** actions added to the app drawer.

### Changed / Fixed
- **Destructive overwrite restore now requires typing `RESTORE`** to confirm
  (restore-as-new stays one-click).
- **`backup_dest` is configurable** (config key; default `/www/server/javahost/backups`).
- **Faster listing** via a sidecar `<archive>.json` manifest (no gzip-open per archive).

### Verified
Live on the box against **MinIO** (full round-trip: test, multi-destination backup,
locations, remote-only restore, delete) and the **scheduled runner** (cron.d line +
local & remote retention). The S3 SigV4 client is confirmed correct (MinIO + a real
Wasabi endpoint accepted the request structure). 169 offline tests + 1 skipped.

## [0.19.0] — 2026-06-07

### Performance (Dashboard + status hot path)
With ~45 apps the Dashboard and the 5 s status poll were slow from per-app
subprocess fan-out and sequential I/O. Optimized end-to-end:

- **Batched `systemctl`:** `list_apps()` now does **one** `systemctl is-active`
  for all units (new `service.status_all`) instead of one per app, and the
  enabled-at-boot check scans the `*.wants` dirs **once** per pass (cached) — was
  N subprocesses + N dir scans.
- **Single-window CPU sampling:** new `instance.metrics_all` resolves all PIDs in
  one `systemctl show` and shares **one** ~0.12 s CPU-sample window across every
  app, so the Dashboard's aggregate CPU is O(0.12 s) total instead of ~0.12 s per
  app (per capped thread-wave).
- **Parallel health probes:** `health_all()` probes apps concurrently — a down
  app no longer serializes the full 2 s timeout × N.
- **Cached `_dir_size`** (60 s TTL): the instances/backups tree is no longer
  fully walked on every Dashboard load.
- **mtime-cached `config.get`:** `config.json` is re-parsed only when it changes
  (read on many paths); removed the stale import-time `proxy.SITE_SUFFIX`.
- **Frontend:** the Dashboard no longer builds the 45-row apps list / DB picker or
  fires `GetHealthAll` while it's the active tab (lazy per-tab render); the 5 s
  apps poll **diffs** the list and skips the DOM rebuild when unchanged (kills the
  periodic flicker); `GetDashboard` is lazy-loaded (no longer fired on every
  refresh from any tab).

No behavior or API changes — purely faster. 159 tests (+5 for the batched
helpers' parsing).

## [0.18.1] — 2026-06-07

### Fixed
- **CI (bandit B314):** the S3 `ListObjectsV2` response parser used
  `xml.etree.ElementTree.fromstring` on the API response. It now refuses any
  DTD/entity declaration before parsing (defense against entity-expansion;
  `etree` does not resolve external entities) — closing the warning without a
  third-party dependency. No behavior change for valid responses.

## [0.18.0] — 2026-06-06

### Added (backup & restore)
- **Per-app backup/restore** (`core/backup/`). A backup archives the app's
  `CATALINA_BASE` (conf, webapp, `bin/` incl. `setenv.sh`/`app.env`/site markers),
  the nginx vhost, and a `manifest.json` — **excluding** `logs/work/temp`, the
  service unit (re-rendered on restore), and **all of `/etc/letsencrypt`** (private
  keys are never bundled; SSL is **re-issued** on restore, best-effort). Restore
  works in place (**overwrite**) or as a **new app** (reallocated port,
  `server.xml`/`app.env` rewrite, domain remap). Archives are `0600` (they contain
  DB credentials). Endpoints: `ListBackups`, `StartBackup`, `StartRestore`,
  `DeleteBackup` (long ops run as async jobs).
- **Hardened tar layer** (`core/backup/archive.py`): the single extraction path
  realpath-contains every member and rejects symlink/hardlink/device/fifo/absolute/
  `..` entries — the defense for restore-from-file.
- **Remote object storage** (`core/backup/s3.py`, `remote.py`): a dependency-free,
  S3-compatible client (stdlib **SigV4**) for **Wasabi / MinIO / Backblaze B2 /
  Cloudflare R2 / AWS** via a custom endpoint. Credentials live in a `0600`
  `remote.json` and the secret key is never returned to the UI. Endpoints:
  `GetRemoteStorage`, `SetRemoteStorage`, `TestRemoteStorage`, `RemoveRemoteStorage`.
- **Scheduled backups + retention** (`core/backup/schedule.py`, `run.py`): per-app
  cron schedules in a managed `/etc/cron.d/javahost-backups` (hardening-aware),
  with local + remote retention. Endpoints: `GetBackupSchedules`,
  `SetBackupSchedule`, `RemoveBackupSchedule`.
- **Restore from upload**: upload a backup `.tar.gz` and restore it
  (`StartRestoreUpload`) — the untrusted archive is unpacked only through the
  hardened extractor.
- **UI**: a **Backups** card on the Applications tab (back up now, restore, restore
  from file, delete) and **Remote storage** + **Scheduled backups** cards in
  Settings.

## [0.17.0] — 2026-06-06

### Added (richer Dashboard)
- **Operational Dashboard aggregates** via a new `GetDashboard` endpoint
  (`core/dashboard.py`), kept separate from the fast `GetStatus` poll so it stays
  cheap. The Dashboard now shows live tiles — **apps running / down / runtime-missing**,
  **aggregate CPU % + RSS** across running apps (parallel `/proc` sampling, capped),
  **apps with SSL + certs expiring <30 days**, and **instances / backups disk usage** —
  plus cards for **Certificates expiring soon** and **Recent tasks**.
- Cert expiry is read from the per-app SSL marker body (new
  `ssl.read_ssl_not_after()`), so the dashboard flags expiring certs with **no
  openssl/network call**. The heavier aggregates lazy-load on dashboard activation
  and manual Refresh only.

## [0.16.2] — 2026-06-06

### Fixed (detail drawer correctness)
- **"Live CPU" showed the thread count, not CPU.** `metrics()` never returned a
  CPU figure, so the drawer's "Live CPU" fell back to `<n> threads`. `metrics()`
  now samples `/proc/<pid>/stat` over a short interval to report a real `cpu_pct`;
  the drawer shows CPU %, plus separate **Threads** and a populated **Uptime** row.
- **Site & SSL alarmed on HTTP-only apps.** A site with no SSL configured showed a
  red "HTTPS unreachable" / "no redirect", as if broken. It now shows neutral
  **"HTTP only" / "not enabled"**; "no redirect" / "unreachable" are flagged as
  errors *only when HTTPS is actually enabled*.
- **`GetSiteStatus` wasted a 3s timeout** probing `https://` on HTTP-only sites
  (nothing listens on :443). The HTTPS probe is now skipped unless the site has a
  cert/SSL — the call dropped from ~3s to ~0.1s.
- The drawer Overview now surfaces the **"runtime missing"** state, and the Metrics
  modal gained a **CPU** row.

## [0.16.1] — 2026-06-06

### Fixed
- **Status no longer lies when a runtime is removed.** If a JDK is force-uninstalled
  while apps use it, those apps keep running on their already-started JVM and showed
  a misleading "up". `list_apps()` now reports **`runtime_ok`** (the pinned
  `JAVA_HOME` still exists), and the UI shows a red **"runtime missing"** badge —
  the app is live but won't survive a restart.
- **Force-uninstalling a JDK now stops its dependent apps** so they go cleanly DOWN
  instead of lingering as zombie JVMs that falsely report healthy.

## [0.16.0] — 2026-06-06

### Changed
- **The plugin no longer detects or reuses aaPanel's `/usr/local/btjdk`.** It now
  manages only its own JDKs under `runtimes/` (plus distro JDKs in `/usr/lib/jvm`),
  so it's fully self-contained — no more confusing "panel JDK" rows or an
  un-removable shared runtime. (Migration on an existing install: repoint any app
  pinned to `btjdk` to a plugin `runtimes/jdk-*`.)

### Fixed
- **Danger-zone "Remove plugin JDKs/Tomcats" no longer breaks running apps.** It
  now SKIPS any JDK/Tomcat still in use by a deployed app (reported as `skipped`);
  a full/apps wipe removes apps first, so everything still clears. Previously a
  JDKs-only wipe could orphan every app's runtime.

### UI/UX
- "Java N is in use" uninstall dialog: leads with a count and a compact, scrollable
  app list (no more giant modal). Databases tab: a live **search/filter** over the
  per-app env chips (+ live count); version lists wrap; driver cells don't overflow.
  Runtimes rows: consistent label/path/badge/button alignment, path shown muted-mono.
  General spacing/overflow consistency pass.

## [0.15.2] — 2026-06-06

### Fixed
- **Oversized icons:** inline icon SVGs rendered huge outside buttons (the Danger-zone
  "FULL WIPE" trash, the panel-JDK "info" badge) because they were only size-
  constrained in button/tab contexts. `ic()` now emits a `.jh-ic` class
  (`1em` square) so icons are consistent everywhere; decorative SVGs (logo,
  empty-state) are untouched. Runtimes/Danger-zone row alignment polished.

### Added
- **Database tab shows the current connection env** (read-only, secret-safe): the
  drawer Database tab now calls `GetDbEnv{app}` and displays engine, connection URL
  (host/port/db — never the password), user, driver, and whether a password is set,
  or "No database env configured". `GetDbEnv` returns no secret.

## [0.15.1] — 2026-06-06

### Fixed
- **JDK (re)install failed with HTTP 403:** the Adoptium API rejects the default
  `python-urllib` User-Agent; the metadata request and the download helper now send
  a real `User-Agent`.
- **JDK install temp-dir cleanup crashed** (`refusing to remove path outside
  managed roots: /tmp/...`): `install_temurin` cleaned its own `mkdtemp` download
  dir via `safe_rmtree` (which only permits managed roots). It now removes that
  private temp dir directly.

## [0.15.0] — 2026-06-06

### Added
- **Java runtime Install / Reinstall / Uninstall** per version (async jobs).
  Uninstall is **blocked when a JDK is in use** by deployed apps (lists the
  dependents; `Force` overrides) — `GetJavaUsage`/`UninstallJava`/
  `StartUninstallJava`/`StartReinstallJava`. The **panel-managed JDK**
  (`/usr/local/btjdk`, shared with aaPanel) is shown with a "panel JDK" badge,
  cannot be uninstalled, and instead offers **"reinstall into the plugin dir"**.
- **Settings → Danger zone:** granular plugin teardown — per-category checkboxes
  (deployed apps / plugin JDKs / Tomcats / reverse-proxy sites / **full wipe**),
  a dry-run **preview** of exactly what will be removed (`WipePreview`), and a
  typed `WIPE` confirmation (`Wipe`). Apps are stopped before removal; the panel
  JDK, panel cert, other plugins' configs, and databases are never touched.
- **Plan-driven uninstall:** `install.sh uninstall` honors an optional
  `/www/server/javahost/.uninstall_plan` (written by the Danger zone) to wipe the
  chosen scope; **default is keep-data** (only the plugin code is removed).

## [0.14.0] — 2026-06-06

Outcome of a full 3-dimension code review (UI / backend / tests-docs).

### Security
- **init.d JAR services no longer shell-source `app.env`** — `. app.env` expanded
  `$(...)`/backticks in values, so a DB password could execute as root on init.d
  hosts. The script now loads vars line-by-line without re-evaluation.

### Added
- **Async app lifecycle:** `StartAppAction{app,action}` runs start/stop/restart/
  repair as background jobs (non-blocking, pollable) — the UI no longer freezes on
  slow systemd operations. Sync `AppAction` kept.
- **SSL / site-status detection:** `GetSiteStatus{app}` reports app health, the
  configured domain, cert presence/validity/expiry (`openssl x509 -enddate`), and
  real HTTP→HTTPS-redirect + HTTPS reachability. Surfaced in the drawer's new
  **Site & SSL** block (cert days-to-expiry warns <14d / errors when expired).
- **`hello.war` dev-info page:** every demo WAR now prints a secret-safe stack
  trace (servlet/Tomcat, Java vendor+version, JVM, OS, filtered JVM args, context,
  request scheme/host, allowlisted sysprops) after the `JAVAHOST_OK` marker.

### Changed
- **De-hardcoded the reverse-proxy domain** (was `5d.bisotech.in` baked into shipped
  code): the suffix is now the `site_suffix` plugin config (empty by default;
  `GetStatus` exposes it; the UI prompts for a domain when unset). No vendor FQDN
  ships in the OSS plugin.

### Fixed
- aaPanel native-SSL/site calls now check the API's returned status instead of
  assuming success, so the **certbot fallback actually runs** when native fails
  (and the nginx-include is always ensured).
- `ssl.enable` always falls back to certbot when native doesn't place a cert;
  certbot errors are surfaced (rate-limit/DNS/challenge) instead of swallowed.
- UI: `looksLikeLoginHtml` no longer treats *any* HTML as session-expiry;
  delete/lifecycle actions are guarded against double-fire and the 5s poll no
  longer wipes in-flight busy state; dead code removed (`metricRow`, stray
  `data-app`); doc viewer got a Close button; dashboard `aria-busy` cleared.
- `ensure_include` matches the nginx `http{}` block safely and rolls back on a
  failed `nginx -t`; `GetProxyHint` is now exception-safe; jobs are pruned.

### Docs / CI
- Documented per-site HTTPS (`SetSiteSSL`) across the docs + skill + README;
  fixed the stale `testbed.md` fixture table (`app.jar`/`boot.jar`); CI gained an
  offline `matrix_full --dry-run` drift check + a `matrix_plan` test; clean-room
  guard now also rejects `tomcat2_main*.pyc`.

## [0.13.2] — 2026-06-06

### Fixed
- **Apps defaulted to the newest JDK (e.g. Java 21) regardless of Tomcat.**
  `instance.create()` resolved the JVM with no preference, and `java.resolve()`
  returns the highest installed JDK ≥ the Tomcat minimum — so every app created
  without an explicit JDK landed on 21. Now, when no JDK is requested, the default
  is the **Tomcat line's baseline** (`min_java`: 9→8, 10.1→11, 11→17), giving
  era-appropriate runtimes.

### Added
- **`CreateApp` accepts a `java` parameter** to pin the JDK per app (the deploy
  matrix already pinned via `prefer_java`; the panel endpoint now exposes it too).

## [0.13.1] — 2026-06-06

### Fixed
- **CI:** bandit flagged the aaPanel-API `request_token` MD5 (B324). MD5 is mandated
  by aaPanel's API token scheme, not a security primitive — marked `# nosec B324`
  (kept cross-Python rather than the `usedforsecurity=` kwarg).

### Changed
- **`dbcheck` demo apps now print rich, secret-safe connection proof:** DB product
  + version, driver name/version, JDBC version, redacted URL, user, catalog/schema,
  connect latency, live `SELECT 1` + DB server time, and the serving stack
  (servlet/Tomcat, Java vendor+version, OS) — `DB_OK` stays the first line so health
  checks are unaffected. Mongo shows a live TCP-probe variant.

## [0.13.0] — 2026-06-06

### Added
- **Per-site HTTPS with a UI toggle.** Each published site (`<app>.5d.bisotech.in`)
  gets an **HTTPS on/off switch** (row + drawer Overview). `SetSiteSSL{app,enable}`
  provisions a cert and rewrites the vhost to a 443 server + 80→443 redirect (+ an
  always-present ACME challenge location for renewal); disabling reverts to HTTP and
  keeps the cert. Strategy: **aaPanel native first** (official API
  `/acme?action=apply_cert_api`, `request_token = md5(request_time + md5(api_sk))`,
  key from plugin config) → **certbot fallback** (`certonly --webroot`) — because
  aaPanel's bundled LE (`sewer`) is broken against pyOpenSSL ≥24 on some hosts. A
  certbot renewal deploy-hook reloads nginx. `list_apps()` now reports `ssl`, and
  `appUrl()` uses `https://` when SSL is on.
- **Database-env form for all engines:** the drawer Database tab now offers an
  engine select (PostgreSQL/MySQL/MariaDB/MongoDB) + host/port/db/user/password and
  an **SSL checkbox** (defaults off for loopback hosts), submitting `SetDbEnv` with
  `db_ssl`, then offering to restart the app.

## [0.12.3] — 2026-06-06

### Performance
- **`GetStatus` was ~1–2s for 8 apps** (UI auto-refreshes every 5s). `list_apps()`
  no longer spawns `systemctl is-enabled` per app (now a filesystem `*.wants`
  symlink stat) nor parses `/proc` metrics per app (uptime is fetched on demand by
  the Metrics drawer). Should drop to <0.2s.
- **Batched health:** new `GetHealthAll` returns `{app:{up,code,port}}` in one call;
  the UI now does one health round-trip per poll instead of N (was one `GetHealth`
  per app).

### Fixed
- **Session-expiry handling:** an expired panel session makes `/plugin` POSTs return
  a 302→login (HTML, not JSON). The UI now detects the non-JSON/redirect response,
  stops all auto-polls, and shows a persistent "Session expired — reload" alert
  instead of silently failing.

## [0.12.2] — 2026-06-06

### Security
- **JAR apps now bind to loopback (127.0.0.1), not `0.0.0.0`.** Like the Tomcat
  connector, executable/Spring-Boot JAR services must not face the public
  interface — they're reached via the reverse proxy. `create_jar` now writes
  `SERVER_ADDRESS`/`SERVER_HOST=127.0.0.1` into the app env (Spring Boot honors
  `SERVER_ADDRESS`; generic apps honor `SERVER_HOST`), and the sample JAR fixtures
  bind the resolved host. Previously a deployed JAR's port was reachable on the
  public IP.

### Fixed
- **JAR app `java` version showed `None`** in the app list: the JAR's `JAVA_HOME`
  lives in `bin/app.env`, which wasn't parsed. `create_jar` now records it there
  and `list_apps()` reads it (new `_read_app_env`).

## [0.12.1] — 2026-06-06

### Fixed
- **Reverse-proxy sites unreachable over IPv6:** the generated nginx vhost only
  had `listen 80;` (IPv4). When a domain has an AAAA record, browsers prefer IPv6
  and got `ERR_CONNECTION_REFUSED`. The vhost template now also emits
  `listen [::]:80;`.
- **"Open" link 404'd on ROOT apps:** the UI appended the context path `/ROOT`,
  but the ROOT webapp is served at `/`. `appUrl()` now maps `/ROOT` → `/`.
- **`SetDbEnv` failed against local non-TLS databases** (`DB_FAIL: server does
  not support SSL`): it always requested SSL. Now honours an explicit `db_ssl`
  flag and otherwise defaults SSL **off for loopback hosts** (127.0.0.1/localhost/::1)
  and on for remote — so local DBs connect out of the box.

## [0.12.0] — 2026-06-06

### Fixed
- **Install "false error":** Install Java/Tomcat ran the large download+extract
  *synchronously* inside the panel AJAX request, so it timed out and flashed an
  error even though the install succeeded. Long ops now run as **detached
  background jobs** (`core/jobs.py`, double-fork + setsid) that return instantly;
  the UI polls status. (The JDKs were always installing correctly.)
- **Dead "Open" link:** app rows linked `http://host:port/`, which can never work
  because connectors bind to **127.0.0.1** by design. "Open ↗" now targets the
  app's reverse-proxy domain; with no domain it offers **Set up reverse proxy**
  instead of a dead link.

### Added
- **Background-job system + endpoints:** `StartInstallJava` / `StartInstallTomcat`
  / `StartUninstallTomcat` → `{job_id}`, `GetJobs`, `GetJobLog`. Jobs persist under
  `/www/server/javahost/jobs/` with state (running/done/failed) + live log.
- **Tasks & Logs UI sections:** a **Tasks** tab (job state · target · elapsed ·
  view-log, auto-polled) and a **Logs** tab (unified app + task log viewer) — full
  WAI-ARIA Tabs wiring, CSP-safe.
- **Reverse-proxy sites:** `SetSite{app,domain?}` / `RemoveSite{app}` create a
  `<app>.5d.bisotech.in` → loopback-port site (aaPanel site API preferred, clean
  nginx-vhost fallback). `list_apps()` now returns the configured `domain`.
- **Full Tomcat×Java×DB testbed:** `tests/e2e/matrix_full.py` (`make matrix`) —
  full cartesian sweep (Tomcat 9/10.1/11 × eligible Java × {none,PG,MySQL,MariaDB,
  Mongo} + JAR×Java×DB = 65 cells), systemd path with service-less fallback,
  `--db-source aapanel|docker`, `--proxy` real-hostname asserts, `--dry-run`.
- **DB demo apps + bytecode pinning:** per-engine `dbcheck.war` (now prints
  `DB_OK <engine> <version>`) and a runnable JDBC `dbapp.jar`; `make_samples.py
  --release {8,11,17,21}` pins `javac --release` to prove Java/runtime binding.
- **Docs:** `docs/testbed.md` on-box campaign guide + testing/skill updates,
  documenting the loopback→reverse-proxy invariant.

## [0.11.0] — 2026-06-06

### Changed
- **Applications redesigned → list + slide-over detail drawer.** Rich rows show
  type (WAR / Spring Boot JAR / Tomcat), runtime chip (Tomcat 11 · Java 17),
  status badge, health pill + port, an inline Start/Stop/Restart segmented control,
  an `Open ↗` link + copy-URL/port, and an overflow menu. Clicking a row opens a
  focus-trapped, Esc-closable drawer with **Overview / Logs / Metrics / Config /
  Database** tabs (only the visible tab polls); reduced-motion aware; works in
  fullscreen. Backed by an enriched `list_apps()` that now returns
  `{type, runtime, tomcat, java, port, context, enabled, backend, uptime}` in one
  `GetStatus` round-trip (derived from cheap stat/reads — no `java -version`/`curl`;
  each app try/except-guarded so one bad instance can't break the list).
- **Section nav moved from the left sidebar back to top tabs** (full-width content),
  implemented as a proper WAI-ARIA Tabs pattern (roving tabindex, arrow/Home/End
  navigation, `role=tab`/`tabpanel`).

### Added
- **Live updates:** the Applications list + health auto-refresh (~5s) while the
  section is visible; paused when hidden / off-section / a modal is open; single
  in-flight; interval cleared on section change. Status taxonomy:
  running / stopped / failed / starting…
- **Open-app link + copy:** `http://<host>:<port><context>` opened in a new tab
  (`rel=noopener`); copy buttons for URL and port (clipboard API + non-HTTPS
  fallback).

### Fixed
- **Health pill stuck on "Checking…"**: row id and lookup now both use `cssId()`
  (was `esc()` on render vs `cssId()` on lookup) — health always resolves.
- **Empty runtime info**: rows now render real type/runtime/port (was always blank
  because `list_apps()` only returned `{app, status}`).
- **Double-fire guard**: row action buttons disable while their call is in flight.
- Restart deduped to a single control; Delete isolated in a danger group + confirm.

## [0.10.0] — 2026-06-06

### Added
- **Fullscreen / full-UI mode:** a toggle in the header pops the plugin out of
  aaPanel's cramped modal to fill the whole viewport using only our own CSS
  (`position:fixed; inset:0`) — the clean-room alternative to a left-sidebar entry,
  which would require patching panel-managed `config/menu.json` (fragile across panel
  updates, against our coexistence stance). Esc exits; focus is managed (WCAG 2.2 AA),
  CSP-safe (no inline handlers).
- **Sample-artifact generator** (`tests/fixtures/make_samples.py`, `make samples`):
  builds `hello.war` (Jakarta JSP → `JAVAHOST_OK`), `legacy.war` (old `javax`
  schema → exercises Migrate), runnable `app.jar` (`com.sun.net.httpserver`),
  `boot.jar` (Spring-Boot-shaped), and `dbcheck.war --db <engine>` (JDBC `SELECT 1`
  → `DB_OK`, driver pulled per `recommend_driver`; Mongo via TCP probe). stdlib
  `zipfile` + `javac` only — no Maven/Gradle.
- **Automated deploy matrix** (`tests/e2e/deploy_matrix.py`, `make test-deploy`):
  service-less install → deploy → health → migrate → JAR-run, with `--with-db`
  spinning ephemeral Docker DBs (postgres/mysql/mariadb/mongo), `SetDbEnv`, and a
  `DB_OK` assertion; full teardown.
- **Opt-in CI** (`.github/workflows/deploy-matrix.yml`): `workflow_dispatch` + weekly
  cron, plus a `deploy-matrix-db` job using GitHub service containers.
- **Testing runbook** (`docs/testing.md`): manual UI walkthrough mapped to real
  endpoints + the automated path.
- **`javahost-test-deploy` skill**: reusable on-demand generate-and-verify runbook.

### Fixed
- **Modal overflow ("hidden env"):** the UI bled off-screen inside aaPanel's modal.
  `.jh` now sizes to the modal box (not `100vh`) and scrolls internally; the classic
  CSS-grid blowout is fixed with `min-width:0` on grid children, and wide content
  (logs, tables, metric rows, long tokens) scrolls/wraps locally instead of widening
  the layout.

## [0.9.1] — 2026-06-06

### Fixed
- Help section threw `TypeError: vers.map is not a function` — `dbGuideCard` assumed
  `versions` was an array but `GetProxyHint` sends a string range; now handles both.
- Help doc links 404'd (panel doesn't serve the repo `docs/`). Docs are now **bundled
  into the plugin** (`plugin/javahost/docs/`) and **rendered on-the-fly** in a modal via
  a new `GetDoc` endpoint (allowlisted + path-traversal-guarded) with a minimal,
  XSS-safe Markdown renderer. Help lists 5 guides (user-guide, system-hardening,
  single-vs-multi-mode, databases-java-apps, troubleshooting).

## [0.9.0] — 2026-06-06

### Added
- **Auto-refreshing metrics:** the per-app Metrics modal now polls `GetMetrics`
  every 4s (in-place updates, auto-refresh toggle, timestamp, pauses when hidden,
  interval cleared on close).
- **`docs/user-guide.md`** — task-oriented walkthrough of every UI section mapped to
  its backend endpoint (+ `docs/images/` placeholder for screenshots).
- **Opt-in git pre-commit hook** (`.githooks/pre-commit`, `make hooks`) — runs the
  offline a11y/CSP lint + py_compile and prints the `javahost-ui` checklist when
  `index.html` changes.

### Changed
- **Database guidance** rewritten and made multi-engine: Help now shows
  PostgreSQL (9.4–18), MySQL (5.5–9.x), MariaDB (10.2–11.x), MongoDB (3.6–8.0) —
  each with version range, default port, recommended driver, and the secret-safe
  `app.env` (DB_URL/DB_USER/DB_PASSWORD) pattern. `GetProxyHint` returns a
  `databases[]` array.

## [0.8.0] — 2026-06-06

### Accessibility (WCAG 2.2 AA / WAI-ARIA APG)
- Darkened secondary-text tokens (`--muted`, `--muted-2`) to meet 4.5:1 contrast.
- Modal: focus trap + focus return to trigger, `aria-describedby` wired.
- Row action menu is now a real `role="menu"` (10 `menuitem`s) with arrow/Home/End/Esc + focus return.
- Sidebar converted from an invalid tablist/tab+aria-current mix to a `<nav>` landmark.
- `prefers-reduced-motion` disables animation/blur; focus-visible on close/menu; aria-labels;
  decorative SVGs `aria-hidden`; error toasts use a separate `role="alert"`/assertive region.

### UX
- Skeleton loaders for stat tiles; hardening "Allow services" is now a primary (not danger) button;
  empty state offers "Deploy Spring Boot JAR"; structured error messages; removed `prompt()` fallback.

### Theming / responsive
- Contrast-safe dark-mode tokens; log/metrics viewer fills modal (single scroll).

### Governance
- New `.claude/skills/javahost-ui/SKILL.md` (clean-room WCAG 2.2 + APG) + offline CI a11y/CSP lint
  (`tests/test_ui_a11y.py`).

## [0.7.2] — 2026-06-06
### Fixed
- UI overflowed off-screen inside aaPanel's modal — root now caps to the viewport with internal
  scroll and the body grid uses `minmax(0,1fr)`. Plugin icon confirmed served (200; hard-refresh past the 1-day cache).

## [0.7.1] — 2026-06-06
### Added
- UI surfaces app metrics (per-app Metrics action) and a Spring profiles field in the JAR modal.

## [0.7.0] — 2026-06-06

### Added
- **Spring Boot / JVM app metrics**: `GetMetrics` endpoint + `instance.metrics()` —
  pid, RSS MB, thread count, uptime from `/proc` (no psutil dependency); pid
  resolved via systemd MainPID or pid-file.
- **Spring profiles** for executable-JAR apps: `CreateJarApp` accepts `profiles`
  (written as `SPRING_PROFILES_ACTIVE` in the app env, validated).
- **Opt-in CI integration job** (`.github/workflows/integration.yml`, manual +
  weekly): installs Tomcat (verified), deploys a WAR, starts it service-less on a
  GitHub runner, and asserts HTTP health — the green systemd-less E2E in CI.

### Changed
- **UI reworked to aaPanel's left-sidebar idiom** (vertical section menu inside the
  plugin pane) while keeping the modern style/colors, cards, modals, toasts, health
  pills; `AllowServices` button wired into the hardening banner.

### Fixed
- **Section navigation** only showed the last tab (Help) — toggled `page-<clicked>`
  on every iteration instead of each page's own id. Now each section switches.
- **Plugin icon** registered at the correct `BTPanel/static/img/soft_ico` path
  (served HTTP 200); `install.sh` handles both panel layouts.

## [0.6.0] — 2026-06-06

### Added
- **`AllowServices`** — one-click auto-whitelist that registers JavaHost in aaPanel
  System Hardening's (`syssafe`) own process allowlist (`process_white` /
  `process_white_rule`): append-only, config backed up, reversible. Registers via
  the sanctioned allowlist — never bypasses (`core/compat/syssafe.py`).
- **Execve-filter detection** — detects the global LD_PRELOAD anti-persistence
  agent (aaPanel `bt_security` / `usranalyse` via `/etc/ld.so.preload`) that causes
  `203/EXEC` "Tips from BT security", and surfaces it (`GetStatus.exec_filter_active`
  + guidance). JavaHost will not disable/patch a global security preload; it reports
  the operator's sanctioned toggle (`usranalyse-disable`/`-enable`).
- `docs/system-hardening.md` rewritten to the full three-layer model.

### Fixed
- Plugin icon: registered at the correct `BTPanel/static/img/soft_ico` path (the
  old path didn't exist); `install.sh` now handles both layouts + ships `ico-javahost.png`.

## [0.5.0] — 2026-06-06

### Added
- **Runs safely under aaPanel System Hardening, no manual disabling.** When a
  service dir is immutable (`chattr +i`), the plugin briefly lifts the bit on its
  own unit path, writes, and **re-locks immediately** (`core/util/immutable.py`),
  preserving hardening. Controlled by `manage_hardening` (default true,
  `core/config.py`). `GetStatus.service_dirs_locked` now reflects true inability.
- **Detects aaPanel daemon/process protection** (`203/EXEC` / "BT security") after
  start and returns a clear, actionable error — JavaHost will not bypass an
  anti-persistence exec filter.
- **Redesigned admin UI**: tabbed dashboard (Dashboard/Applications/Runtimes/
  Databases/Help), stat tiles, health pills, per-row action menus, modal dialogs
  for create/deploy/JAR/DB/logs, toasts, busy states — self-contained, no CDNs.
- Docs: `docs/single-vs-multi-mode.md`; `system-hardening.md` documents both layers.

### Changed
- init.d run-as uses `runuser` (not the aaPanel-jailed `su`); systemd unit gains `PIDFile=`.

## [0.4.0] — 2026-06-06

### Added
- **Spring Boot / executable-JAR apps**: `core/deploy/jar.py` (Main-Class +
  Spring Boot fat-jar detection), `instance.create_jar()`, systemd + init.d jar
  service templates, and `CreateJarApp` endpoint — runs `java -jar` as a service
  with `SERVER_PORT`.
- **Health endpoint**: `instance.health()` + `GetHealth` endpoint (loopback HTTP
  probe; UI shows a green/red badge per app).
- **WAR upload + Jakarta-migrate UI** and a **Spring Boot JAR** card (file inputs
  wired to `UploadWar`/`MigrateWar`/`CreateJarApp`).
- **System-hardening awareness**: `GetStatus` now reports `service_dirs_locked` +
  a hint; the dashboard shows a banner; new `docs/system-hardening.md` documents
  how to run JavaHost when aaPanel System Hardening is enabled.
- **CI security gates**: bandit (medium+, blocking, with a documented `.bandit`),
  pip-audit, and shellcheck made blocking; all CI tool versions pinned.

### Changed
- `installer._keyring` logs explicitly when GPG verification is downgraded to
  SHA-512-only (gpg absent / KEYS import failed) — never a silent skip; SHA-512
  remains mandatory and a present-but-bad signature still hard-fails.

## [0.3.0] — 2026-06-06

### Added
- `UploadWar` + `MigrateWar` endpoints. `war.migrate()` runs the Apache Tomcat
  Migration Tool for Jakarta EE (verified download, SHA-512 fail-closed) to convert
  `javax.*` WARs for Tomcat 10/11.
- Port allocation + conflict detection (`instance.allocate_port`/`port_in_use`/
  `used_ports`) — closes compatibility-matrix item B5; `CreateApp` now rejects a
  taken port or auto-picks a free one.
- End-to-end smoke harnesses (`tests/e2e/`): full systemd chain + a service-less
  variant for hardened hosts. Validated on Ubuntu 24.04 — Tomcat 11 serves a
  deployed app on the auto-allocated loopback port.

### Fixed (found by real-host E2E)
- Service install detects immutable/locked service dirs (e.g. aaPanel "System
  Hardening" `chattr +i`) and raises a clear, actionable error instead of EPERM;
  per-app systemd/init.d backend resolution keeps fallback consistent.
- Installer makes shared `CATALINA_HOME` group/other `r-X` so the `www` run-user
  can execute `catalina.sh` (Apache tar ships `bin/*.sh` as 0750).
- Each per-app `CATALINA_BASE` now gets the default `conf/web.xml` (DefaultServlet
  + welcome-files) and is chowned to the run user — fixes a `/` → 404 on deploy.

## [0.2.0] — 2026-06-06

### Added
- App lifecycle endpoints + `core/tomcat/instance.py` (per-app CATALINA_BASE):
  `CreateApp`, `AppAction`, `UpdateTomcat`, `DeleteApp`, `RepairApp`,
  `GetAppDetail`, `GetLogs` (memory-safe log tail), with marker-gated removal.
- Full clean-room admin UI (`index.html`): dashboard, app create/actions/logs,
  multi-engine DB helper, reverse-proxy hint — XSS-escaped, dependency-light.
- Documentation set: `docs/architecture.md`, `java-runtime.md`, `tomcat-10.md`,
  `tomcat-11.md`, `databases-java-apps.md`, `troubleshooting.md`,
  `aaPanel-plugin-packaging.md`, plus `INSTALL.md`, `CONTRIBUTING.md`, `SECURITY.md`.
- Project Agent Skills under `.claude/skills/`: `javahost-dev`, `javahost-release`,
  `javahost-security` (clean-room, instructions-only).

### Changed
- Entrypoint slimmed: `CreateApp`/`DeployWar` delegate to `instance.py`.

## [0.1.0] — 2026-06-06

First release of **JavaHost**, an independent clean-room rewrite. This project
replaces an earlier prototype that was a fork of aaPanel's proprietary `tomcat2`
plugin; that code has been removed and the git history reset so no aaPanel
source is present in any commit (see `docs/audit/`).

### Added
- Clean-room plugin `javahost` (Apache-2.0), built only against the panel's
  public plugin API — contains no aaPanel source, UI, or assets.
- Tomcat version registry with dynamic latest-patch resolution for 9 / 10.1 / 11.
- **Integrity-verified** Tomcat & JDK downloads (SHA-512 + OpenPGP, fail-closed),
  with offline (local tarball + hash) support.
- Java runtime layer: detect 8/11/17/21, install Temurin 17/21, robust
  `java -version` parsing, JVM-flag validation for Java 17/21.
- **systemd** service generation (init.d fallback); `JAVA_HOME` supplied via
  environment, never parsed from a script line.
- Security hardening on install: remove examples/docs/manager webapps, no AJP,
  shutdown port disabled, runs as `www`, locked-down config permissions.
- Zip-slip-safe WAR deployment; `javax`→`jakarta` namespace detection/warnings.
- Plugin-owned Nginx reverse-proxy vhost generator.
- **Multi-database connectivity helpers** (all practical versions): PostgreSQL
  (9.4–18), MySQL (5.5–9.x), MariaDB (10.2–11.x), MongoDB (3.6–8.0) — connection-
  URL builder, JVM→driver matrix, local-server detection, and a secret-safe
  `app.env` (0640, no credentials in the URL/WAR/logs). See `docs/databases-java-apps.md`.
- Idempotent install with atomic staging + rollback, disk precheck, and
  managed-marker uninstall.
- Offline unit test suite (pytest) and CI (lint + tests + signed-zip artifact).
