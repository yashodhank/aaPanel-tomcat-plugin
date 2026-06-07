---
name: javahost-dev
description: >
  Conventions + build/test/deploy runbook for the JavaHost aaPanel plugin (this
  repo). Use when working anywhere under plugin/javahost/, core/, tests/, or docs/
  — adding endpoints, editing the installer/runtime/tomcat/db engines, writing
  tests, or deploying to a panel. Enforces the clean-room rule and project layout.
allowed-tools: Bash(python3 -m pytest *) Bash(python3 -m py_compile *) Bash(make test) Bash(make lint)
---

# JavaHost — developer guide

JavaHost is an **independent, clean-room, Apache-2.0** Tomcat/Java runtime manager
plugin for aaPanel/BaoTa-style panels. It is **not** a fork of aaPanel's `tomcat2`.

## Clean-room rule (non-negotiable)
- **Never** copy aaPanel/BaoTa source, UI markup, layout, icons, or strings into
  this repo. The AAPANEL license forbids public release of derived code (§2.2/§4.3);
  independent API-only plugins are allowed (§3.1).
- The proprietary reference lives in `_private_spec/` (gitignored) — read for
  behavior only, never paste. CI fails if `tomcat2_main.py` is ever tracked.
- The only module allowed to touch panel internals is `core/compat/aapanel.py`.

## Layout
```
plugin/javahost/
  javahost_main.py     # thin aaPanel entrypoint; class javahost_main; methods take `get`, return panel.ok/err
  info.json            # manifest (name MUST be "javahost")
  index.html icon.svg  # original UI + icon
  install.sh tomcat_install.sh
  core/
    util/   shell.py(arg-list exec) validate.py download.py(sha512+gpg) fs.py(atomic+markers)
    runtime/ java.py(detect/install/reinstall/uninstall 8/11/17/21; self-contained runtimes/, no btjdk) jvm_opts.py
    tomcat/  registry.py installer.py service.py hardening.py instance.py templating.py templates/*.tmpl
    deploy/  war.py(zip-slip-safe) proxy.py(<app>.<site_suffix>) ssl.py(LE: native ACME->certbot) sitestatus.py
    db/      _base.Engine + pg/mysql/mongo + engines.py (registry)
    compat/  aapanel.py syssafe.py(hardening allowlist)
    jobs.py(detached bg jobs) maintenance.py(Danger-zone wipe) config.py(site_suffix, manage_hardening)
  docs/      bundled docs served in-UI via GetDoc (KEEP IN SYNC with repo docs/: user-guide, system-hardening, single-vs-multi-mode, databases-java-apps, troubleshooting)
tests/                 # offline pytest (no panel, no network)
docs/                  # endpoints, architecture, java-runtime, tomcat-10/11, databases-java-apps, system-hardening, troubleshooting, single-vs-multi-mode, testing, testbed, packaging
```

## Coding conventions (enforced)
- **No shell string-building.** Run commands via `core.util.shell.run([...])` arg-lists; `shell=True` is banned.
- **Validate every `get.*` input** through `core.util.validate` before it touches fs/shell/templates.
- **Filesystem:** `fs.atomic_write` (explicit mode), `fs.safe_rmtree` (marker- + root-gated), `fs.mark_managed` on anything you create. Secure temp via `fs.mkdtemp`.
- **Downloads** go through `util.download.fetch_verified` (SHA-512 mandatory, GPG when available, fail-closed). Never add an unverified download path.
- **Templates** use `@@token@@` placeholders + `tomcat.templating` (so shell/XML `${...}` is untouched). Missing token = hard error.
- **DB engines**: add new engines by subclassing `db._base.Engine` and registering in `db/engines.py`.
- Endpoints: add a method to `javahost_main`, validate inputs, delegate to `core/`, return `panel.ok(data)` / `panel.err(msg)`. Keep the entrypoint thin. Full method catalogue: `docs/endpoints.md`.
- **Long ops are async jobs.** Heavy work (JDK/Tomcat install/uninstall, app lifecycle) goes through `core.jobs` (`StartInstall*`/`StartUninstall*`/`StartReinstallJava`/`StartAppAction` → `{job_id}`, polled via `GetJobs`/`GetJobLog`). Keep a sync variant for CLI where one already exists.
- **Secret-safe DB env**: `SetDbEnv` writes `app.env` (0640) and never echoes secrets; `GetDbEnv` returns the URL/user/driver and `has_password` only — never the password.
- **Bundled docs ↔ repo docs.** The five `GetDoc`-allowlisted files exist in BOTH `docs/` and `plugin/javahost/docs/`; when you change one, update its twin so the in-UI Help matches the repo.

## Test / lint (always before commit)
```bash
make test      # py_compile all + pytest tests/   (offline; must stay green)
make lint      # shellcheck plugin/javahost/*.sh + py_compile
```
Add a unit test for any new pure logic (parsers, URL builders, validators, templating, zip-slip). Tests must not hit the network or a real panel.

## Deploy to a panel (your own box only)
```bash
make deploy VPS_HOST=root@<host>     # rsync plugin/javahost + restart panel
```
Ops/login creds for the test box are in `_private_spec/OPS-ACCESS.md` (gitignored).

## More detail
- Endpoints: `docs/endpoints.md` · Architecture: `docs/architecture.md` · Java: `docs/java-runtime.md`
- Tomcat 10/11 (and why 11 isn't drop-in): `docs/tomcat-10.md`, `docs/tomcat-11.md`
- Databases: `docs/databases-java-apps.md` · Hardening: `docs/system-hardening.md` · Packaging: `docs/aaPanel-plugin-packaging.md`
- Testing: `docs/testing.md` (+ full on-box campaign `docs/testbed.md`); deploy/DB matrix: `javahost-test-deploy` skill.
- UI edits: `javahost-ui` skill. Releasing: `javahost-release` skill. Security review: `javahost-security` skill.

## Backup / storage model (v0.18+, multi-destination v0.20+)
- `core/backup/`: `archive.py` (the ONLY tar extractor — realpath-contain + reject
  symlink/hardlink/device/`..`/absolute), `store.py` (backup/restore/list/prune;
  archives `0600`, exclude logs + ALL `/etc/letsencrypt`; sidecar `<archive>.json`
  for cheap listing; `_backups_root()` honors config `backup_dest`), `s3.py`
  (dependency-free SigV4 client, custom endpoint, path-style), `remote.py` (a
  **registry of named profiles** in `0600` `remotes.json`; legacy `remote.json`
  auto-migrates to a `default` profile), `schedule.py`+`run.py` (cron.d + retention).
- Multi-destination: `backup_app(app, remotes)` (csv ids / `"all"`) fans out and
  records per-destination results; `list_backups` tags each backup with `locations`
  (local + each profile holding it); `restore`/`ensure_local` take a source `profile`.
- **UI param gotcha:** a storage profile's display name MUST travel as `label`, never
  `name` — aaPanel's router treats a POST `name` as the plugin/module name and
  rejects values with spaces/symbols ("module_name ... cannot contain special symbols").
- The whole feature lives under the dedicated **Backups** top-tab. Docs: `docs/backup-restore.md`.
