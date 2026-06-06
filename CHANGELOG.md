# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

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
