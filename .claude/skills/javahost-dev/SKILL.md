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
    runtime/ java.py(detect/install 8/11/17/21) jvm_opts.py
    tomcat/  registry.py installer.py service.py hardening.py instance.py templating.py templates/*.tmpl
    deploy/  war.py(zip-slip-safe) proxy.py
    db/      _base.Engine + pg/mysql/mongo + engines.py (registry)
    compat/  aapanel.py
tests/                 # offline pytest (no panel, no network)
docs/                  # architecture, java-runtime, tomcat-10/11, databases-java-apps, troubleshooting, packaging
```

## Coding conventions (enforced)
- **No shell string-building.** Run commands via `core.util.shell.run([...])` arg-lists; `shell=True` is banned.
- **Validate every `get.*` input** through `core.util.validate` before it touches fs/shell/templates.
- **Filesystem:** `fs.atomic_write` (explicit mode), `fs.safe_rmtree` (marker- + root-gated), `fs.mark_managed` on anything you create. Secure temp via `fs.mkdtemp`.
- **Downloads** go through `util.download.fetch_verified` (SHA-512 mandatory, GPG when available, fail-closed). Never add an unverified download path.
- **Templates** use `@@token@@` placeholders + `tomcat.templating` (so shell/XML `${...}` is untouched). Missing token = hard error.
- **DB engines**: add new engines by subclassing `db._base.Engine` and registering in `db/engines.py`.
- Endpoints: add a method to `javahost_main`, validate inputs, delegate to `core/`, return `panel.ok(data)` / `panel.err(msg)`. Keep the entrypoint thin.

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
- Architecture: `docs/architecture.md` · Java: `docs/java-runtime.md`
- Tomcat 10/11 (and why 11 isn't drop-in): `docs/tomcat-10.md`, `docs/tomcat-11.md`
- Databases: `docs/databases-java-apps.md` · Packaging: `docs/aaPanel-plugin-packaging.md`
- Releasing: use the `javahost-release` skill. Security review: `javahost-security` skill.
