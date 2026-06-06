# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

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
