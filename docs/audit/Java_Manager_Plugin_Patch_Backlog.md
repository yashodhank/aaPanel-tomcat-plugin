# Java Manager — Plugin Patch Backlog

**Date:** 2026-06-06
**Legend — Priority:** P0 (blocker) · P1 (high) · P2 (medium) · P3 (low). **Risk:** Low/Med/High.
Maps to finding IDs (F1–F14) from the audit report and phases (M) from the strategy.

---

## Phased plan summary

| Phase | Goal | Gate to next |
|-------|------|--------------|
| 0 | Safety, license, repo containment, repo map | Repo private/clean; LICENSE; artifacts ignored |
| 1 | Behavior extraction → private spec | `docs/spec/` complete, no code copied |
| 2 | New OSS plugin skeleton (manifest, entrypoint, compat boundary) | Loads in panel, non-destructive |
| 3 | Java runtime detection layer (8/11/17/21) | `java.py` unit-tested |
| 4 | Tomcat 9 legacy compat (isolated) | Legacy flag, no impact on 10/11 |
| 5 | Tomcat 10 support | Install+start verified on Ubuntu 24.04 |
| 6 | Tomcat 11 support | Install+start verified, Java 17 gate |
| 7 | App deploy + reverse proxy | WAR deploy + Nginx vhost works |
| 8 | PostgreSQL 17 Java-app guidance + env | JDBC template + env-file |
| 9 | Security hardening | F2/F3/F9 closed |
| 10 | UI/UX polish | Dashboard + actions |
| 11 | Tests, docs, packaging, release | CI green, docs complete, signed release |

For each phase below: **goal · files · safe validation · expected output · rollback · risks.**

---

## Backlog table

| Priority | Phase | Task | Files | Risk | Validation | Notes |
|----------|-------|------|-------|------|-----------|-------|
| P0 | 0 | Make repo private OR purge copied files from history | repo settings; `git filter-repo` | Med | `gh repo view --json visibility`; `git log -- tomcat2_main.py` empty | Closes **F1**. Do first. |
| P0 | 0 | Remove aaPanel-derived files locally→spec; stop tracking | `tomcat2_main.py`,`index.html`,`info.json`,`icon.png` | Low | `git ls-files \| grep -E 'tomcat2_main\|index.html'` empty | Keep private copy as spec only. |
| P0 | 0 | Add LICENSE (Apache-2.0) for original code | `LICENSE`,`NOTICE` | Low | `gh repo view --json licenseInfo` non-null | **F1**. |
| P1 | 0 | `.gitignore` + untrack build artifacts | `.gitignore`; `graphify-out/`,`.graphify_*`,`*.zip` | Low | `git ls-files \| grep graphify` empty | **F11**. |
| P1 | 1 | Extract behavior spec (methods, IO, side-effects) | `docs/spec/*.md` (private) | Low | Spec covers every old public method | No code paste. |
| P1 | 2 | New manifest with unique identity | `info.json` | Low | `python -c "import json;json.load(open('info.json'))"`; name≠tomcat2, id≠10000 | **F1** coexistence. |
| P1 | 2 | Compat boundary module (only panel-coupled code) | `core/compat/aapanel.py` | Med | import-time smoke; no destructive calls | Isolates `public.ExecShell`. |
| P1 | 2 | Safe shell helper (arg-list only) | `core/util/shell.py` | Med | unit test rejects shell metachars; no `shell=True` | Kills **F3** class. |
| P0 | 3 | JDK detect + robust version parse (8/11/17/21) | `core/runtime/java.py` | Med | unit test parses Temurin/Zulu/Oracle banners; `-XshowSettings:properties` | **F10**. |
| P1 | 3 | JDK install for 17 **and 21** w/ checksum+gpg | `core/runtime/java.py`,`download.py` | Med | offline test: tampered tar rejected | **F2/F6/F10**. |
| P1 | 3 | JVM flag validator (strip unsupported on 17/21) | `core/runtime/jvm_opts.py` | Low | unit: `UseConcMarkSweepGC`,`MaxPermSize` removed for 17 | **F10**. |
| P1 | 4 | Tomcat 9 legacy path, isolated behind flag | `core/tomcat/registry.py` | Low | registry test: `legacy=True`, no 10/11 coupling | Keep separate. |
| P0 | 5 | Tomcat version registry + dynamic patch resolver | `core/tomcat/registry.py`,`installer.py` | Med | resolver returns 10.1.x; no hardcoded pin | **B2/F6**. |
| P0 | 5 | Installer: download→**sha512+gpg verify**→extract | `core/tomcat/installer.py`,`download.py`; ship `KEYS` | High | tampered tarball → fail closed; good tarball → ok | **F2**. |
| P0 | 5 | server.xml/context/setenv templates (Jinja, per major) | `core/tomcat/templates/*.j2` | Med | render unit test diff vs golden | **F13**. |
| P0 | 5 | systemd unit gen + init.d fallback; JAVA_HOME via env | `core/tomcat/service.py` | High | `systemd-analyze verify` on generated unit; dry-run render | Fixes **B1/F4**, removes jsvc dep **B3**. |
| P1 | 5 | Hardening pass (rm examples/docs, manager off, shutdown -1, no AJP) | `core/tomcat/hardening.py` | Med | post-install `ls webapps` lacks examples/docs; assert no AJP connector | **F9**. |
| P1 | 5 | Disk precheck + atomic staging + rollback | `installer.py`,`util/fs.py` | Med | simulate low disk → abort before mutate | **F12/R2**, rollback §8. |
| P1 | 5 | Managed-marker on install; uninstall checks it | `installer.py` (`.javahost-managed`) | Med | uninstall refuses unmarked dir | **F14/R3**. |
| P0 | 6 | Tomcat 11 support + Java 17 floor gate | `registry.py`,`installer.py`,`java.py` | Med | install T11 on Java 11 → refused with clear msg | min_java=17. |
| P1 | 6 | javax→jakarta WAR scan + warning | `core/deploy/war.py` | Med | WAR with `javax.servlet` on T11 → warning | **F5**. |
| P2 | 6 | Integrate Jakarta migration tool (opt-in) | `core/deploy/war.py` | Med | migrate sample WAR offline | **F5**. |
| P1 | 7 | WAR/exploded deploy, **zip-slip-safe** | `core/deploy/war.py` | High | unit: entry `../x` rejected | Security. |
| P1 | 7 | Port registry + conflict detection (replace 8080–8084) | `core/deploy/proxy.py`,`tomcat/service.py` | Med | allocate >5 apps; no collision; checks listening sockets | **B5/F8**. |
| P1 | 7 | Plugin-owned Nginx vhost; backup-before-write shared includes | `core/deploy/proxy.py` | Med | generated conf in plugin dir; `nginx -t` | **F8** coexistence. |
| P1 | 7 | Health check endpoint poll | `core/deploy/war.py` | Low | poll returns status w/ timeout | Reliability. |
| P2 | 8 | PostgreSQL 17 JDBC guidance + connection template | `core/db/pg.py`,`docs/postgresql-java-apps.md` | Low | render JDBC URL; no creds echoed | Guidance only. |
| P2 | 8 | App env-file support (`app.env`, 0640, no secrets in WAR) | `core/deploy`,`service.py` | Med | env file perms 0640; values escaped | Secret-safe. |
| P0 | 9 | Replace all interpolated shell with arg-lists + validation | all `core/*` | High | grep `ExecShell(.*%` = 0; injection unit tests | **F3**. |
| P1 | 9 | Remove `/etc/hosts` edits | (none — design omits) | Low | grep `/etc/hosts` = 0 | **F8**. |
| P1 | 9 | Secure temp files (`mkstemp`, 0600) | `util/fs.py` | Low | no predictable `/tmp/*.pl` | **F7**. |
| P2 | 10 | Dashboard: Java/Tomcat status, versions, ports, logs viewer | `index.html`,`static/` | Med | UI loads; XSS-escaped output | Original UI (**F1**). |
| P2 | 10 | Actions: install/update/remove/start/stop/restart/repair | UI + endpoints | Med | each action dry-runnable | UX. |
| P2 | 10 | Compatibility warnings + diagnostic export | UI + `core/util` | Low | export bundle excludes secrets | UX. |
| P1 | 11 | Unit tests (templates, parsers, shell-safety, zip-slip) | `tests/` | Med | `pytest` green in CI | No prod. |
| P1 | 11 | Docs set (README/INSTALL/SECURITY/CHANGELOG/CONTRIBUTING + docs/*) | `*.md`,`docs/` | Low | links resolve; lint | Section J. |
| P1 | 11 | CI: lint (`shellcheck`,`ruff`), syntax, package, **signed** release | `.github/workflows/*` | Med | CI green; release asset signed | Packaging. |

---

## Per-phase detail (goal · files · validation · expected · rollback · risks)

**Phase 0 — Safety/license/repo map.**
Goal: stop the license violation. Files: repo settings, `LICENSE`, `.gitignore`. Validation: `gh repo view --json visibility,licenseInfo`; `git ls-files`. Expected: private or clean repo, LICENSE present, no copied files tracked. Rollback: n/a (additive). Risks: history purge needs force-push coordination.

**Phase 1 — Behavior extraction.**
Goal: spec without code. Files: `docs/spec/`. Validation: every old public method documented (inputs/outputs/side-effects). Expected: spec complete. Rollback: n/a. Risks: accidental code paste — review diffs.

**Phase 2 — Skeleton.**
Goal: loadable, inert plugin. Files: `info.json`,`*_main.py`,`core/compat`,`core/util/shell.py`. Validation: JSON valid; import smoke; no destructive calls. Expected: plugin appears in panel, no-ops safely. Rollback: remove plugin dir. Risks: panel API drift — pin to documented contract.

**Phase 3 — Java runtime.**
Goal: detect/install 8/11/17/21. Files: `core/runtime/*`,`download.py`. Validation: parser unit tests across vendor banners; tampered-tar rejection. Expected: correct JAVA_HOME per runtime. Rollback: per-runtime dirs removable. Risks: vendor banner variance.

**Phase 4 — Tomcat 9 legacy.**
Goal: isolated legacy. Files: `registry.py`. Validation: registry test. Expected: 9 installable, flagged legacy, no 10/11 coupling. Rollback: remove line. Risks: scope creep — keep minimal.

**Phase 5 — Tomcat 10.**
Goal: primary modern path with verify+systemd+hardening. Files: `installer.py`,`service.py`,`hardening.py`,`templates/`. Validation: `systemd-analyze verify`; render diffs; tampered-tar fail-closed; `ls webapps`. Expected: T10 installs, starts as `www`, hardened. Rollback: atomic staging discard. Risks: GPG/sha tooling absence — fail closed + document.

**Phase 6 — Tomcat 11.**
Goal: modern optional + Java 17 gate + namespace warn. Files: `registry.py`,`java.py`,`deploy/war.py`. Validation: refuse on Java<17; javax-WAR warning. Expected: T11 on Java 17/21. Rollback: as phase 5. Risks: app incompat — surface clearly.

**Phase 7 — Deploy + proxy.**
Goal: WAR deploy, ports, vhost, health. Files: `deploy/*`. Validation: zip-slip unit; `nginx -t`; port allocator. Expected: app reachable via Nginx. Rollback: remove instance + vhost. Risks: foreign-config edits — only plugin-owned files.

**Phase 8 — PostgreSQL 17.**
Goal: guidance + env. Files: `db/pg.py`,`docs/`. Validation: JDBC render; perms. Expected: copy-paste-ready config, no secrets leaked. Rollback: n/a. Risks: scope — guidance only, not a DB manager.

**Phase 9 — Security hardening.**
Goal: close F2/F3/F7/F9. Files: all core. Validation: grep interpolation=0; injection tests. Expected: no shell string-building. Rollback: per-commit. Risks: regressions — strong tests.

**Phase 10 — UI.**
Goal: original dashboard. Files: `index.html`,`static/`. Validation: XSS-escape tests. Expected: status + actions. Rollback: revert UI. Risks: reintroducing aaPanel layout — write fresh.

**Phase 11 — Tests/docs/release.**
Goal: ship. Files: `tests/`,docs,CI. Validation: CI green; signed asset. Expected: installable release. Rollback: yank release. Risks: unsigned/unverified artifacts.
