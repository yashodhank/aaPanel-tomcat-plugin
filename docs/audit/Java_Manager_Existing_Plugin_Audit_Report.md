# Java Manager — Existing Plugin Audit Report

**Date:** 2026-06-06
**Auditor:** automated source audit (read-only; no production actions)
**Subjects audited:**

1. **Public OSS repo (current):** `github.com/yashodhank/aaPanel-tomcat-plugin` — local clone `~/Projects/aaPanel-tomcat-plugin/`
2. **Upstream proprietary reference:** aaPanel panel source `~/Projects/aaPanel/` (`origin = github.com/aaPanel/aaPanel.git`), branches `feat/tomcat-java-pgsql-runtime` (HEAD) and `feature/native-tomcat-pgsql` (worktree).

> **Scope note:** the "Java Manager / Java Project Manager" is **aaPanel's official `tomcat2` plugin** plus the panel's built-in Java subsystem (`mod/project/java/`, `class_v2/projectModelV2/javaModel.py`, `class/tomcat.py`, `install/tomcat_install.sh`, `class_v2/adapters/tomcat_adapter.py`). There is no independent third-party "Java Manager" — it is first-party aaPanel code.

---

## 1. Repo map

### 1a. Public OSS repo (`aaPanel-tomcat-plugin`)
| File | Lines | Role | Provenance |
|------|------:|------|-----------|
| `tomcat2_main.py` | 1674 | Plugin backend controller (install/start/stop/site/port/jdk) | **Copied from aaPanel `plugin/tomcat2/main.py`** — 1478/1674 lines identical |
| `index.html` | 1292 | Plugin UI | Copied from aaPanel tomcat2 (links to `bt.cn` forum, `index.html:149`) |
| `info.json` | 17 | aaPanel plugin manifest | `name:"tomcat2"`, `id:10000` (aaPanel's IDs), `author:"community-fork"` |
| `install.sh` | 26 | Plugin install/uninstall hook | aaPanel plugin convention |
| `tomcat_install.sh` | 236 | Generic Tomcat 10/11 installer (new) | New work (also present verbatim in `aaPanel/install/tomcat_install.sh`) |
| `icon.png` | — | `ico-tomcat2.png` | aaPanel asset |
| `README.md`, `CHANGELOG.md`, `Makefile`, `scripts/release.sh`, `.github/workflows/{ci,release}.yml` | — | OSS packaging (new, good) | Original |
| `graphify-out/`, `.graphify_*`, `aaPanel-tomcat-plugin.zip` | — | **Build/tool artifacts committed by mistake** | Should be `.gitignore`d |

### 1b. Upstream aaPanel Java subsystem (reference only — never copy)
| File | Lines | Role |
|------|------:|------|
| `class_v2/projectModelV2/javaModel.py` | ~6k | Primary Java/Tomcat model (94 `ExecShell` calls) |
| `mod/project/java/projectMod.py` | 134k bytes | Java project controller |
| `mod/project/java/utils.py` | 34k bytes | `TomCat`, `JDKManager` helpers (16 `ExecShell`) |
| `mod/project/java/{server_proxy,java_web_conf,springboot_parser,groupMod,project_update}.py` | — | Proxy, vhost conf, Spring Boot detection, grouping |
| `class/tomcat.py` | — | Legacy `server.xml` ElementTree manipulator (2015–2017) |
| `class_v2/adapters/{tomcat_adapter,deployment_adapter,pgsql_adapter}.py` | — | v2 contract adapters wrapping the above |
| `install/tomcat_install.sh` | 236 | Same script as the OSS repo's |

---

## 2. License status — **BLOCKING**

**Classification: License unclear-but-restrictive → Clean-room rewrite REQUIRED. Do NOT copy implementation code into any publicly released artifact.**

Evidence (`~/Projects/aaPanel/license.txt`, "AAPANEL Open Source License Agreement"):
- **§2.2** — personal modification allowed *"on the premise of retaining the copyright mark, **but it is not allowed to be publicly released.**"*
- **§2.3** — forbids "undermin[ing] the commercial authorization mechanism" / running same-type services using aaPanel source.
- **§4.3** — when integrating/publishing aaPanel, "the user shall not make any changes to the aaPanel source code."
- **§5** — all IP reserved (text, graphics, interface design, layout); **§9.2** PRC law, Dongguan court.
- All source files: `Copyright (c) 2015-2099 aapanel Software … All rights reserved.` (e.g. `javaModel.py:5`, `tomcat_adapter.py:5`, `class/tomcat.py:6`).
- **Escape hatch — §3.1/§3.2:** aaPanel **explicitly permits** "API-based applications … released for free or for a fee," provided they "shall not contain any damage or modification of the aapanel code." → An **independent plugin you author that contains zero aaPanel source** is permissible.

**Current violation (confirmed):** the **public** repo `aaPanel-tomcat-plugin` is a verbatim 88%-identical fork of the proprietary `tomcat2` plugin (`tomcat2_main.py`: 1478/1674 identical lines), self-described in `README.md:3` as *"Patched fork of the official tomcat2 (Java Project Manager) plugin,"* with **no LICENSE file** (`gh repo view` → `licenseInfo: null`). This breaches §2.2 and §4.3 on a public repository.

**Required containment (do first, before any feature work):**
1. Make the repo **private**, or remove `tomcat2_main.py`, `index.html`, `info.json`, `icon.png` (and rewrite git history to purge them) until clean-room replacements exist.
2. Keep the copied files **locally only** as a *behavioral specification* to rebuild from — never re-commit them publicly.
3. Add a `LICENSE` (Apache-2.0 or MIT recommended) covering **only your original code** once the rewrite lands.

---

## 3. Current plugin behavior (extracted)

- **Plugin model:** aaPanel "environment" plugin `tomcat2`, dispatched by the panel; methods receive a `get` namespace object (`info.json` → `shell:"tomcat.sh"`, `checks:"/www/server/panel/plugin/tomcat2"`).
- **Tomcat versions:** class attributes for 7/8/9/10/11 paths, init scripts, server.xml, logs (`tomcat2_main.py:41–63`). 10/11 added by this fork.
- **Service management:** **SysV init.d**, not systemd. Per-version `/etc/init.d/tomcat{N}` + `bttomcat{N}` symlink; restart = `"/etc/init.d/%s stop && /etc/init.d/%s start"` (`tomcat2_main.py:418`).
- **Per-site model:** each Java "site" gets its own Tomcat `CATALINA_BASE` under `/www/server/tomcat_site/<domain>/` with its own `server.xml` edited via ElementTree (`tomcat2_main.py:450,552,1038–1058`), `daemon.sh start/stop` per site (`399–406`).
- **Port allocation:** scans `range(8080, 8085)` (`tomcat2_main.py:92`); socket probe binds `0.0.0.0` (`1630`).
- **Install flow:** `install.sh` copies icon + creates plugin dir; `tomcat_install.sh install <ver> [jdk]` downloads Tomcat + (optionally) JDK 17, builds `jsvc`, writes init script, creates symlinks.
- **JDK handling:** resolves from `/usr/local/btjdk/jdk{21,17,11,8}` or `/www/server/java/jdk-*`, else downloads Temurin 17 from Adoptium (`tomcat_install.sh:60–125`). Per-version min-JDK enforced (`130–134`).

---

## 4. Confirmed bugs

| # | Bug | Evidence | Effect |
|---|-----|----------|--------|
| B1 | Generated init.d reads `JAVA_HOME` from **line 1 (shebang)** of `daemon.sh`, but installer inserts it at **line 2** | `tomcat_install.sh:177` (`sed '2i…'`) vs `:215` (`head -1 … \| sed 's/JAVA_HOME=//'`) | init script exports a garbage `JAVA_HOME` (the shebang); Tomcat may start with wrong/again-resolved Java or fail. The two git fixes ("insert at line 2" vs "regex match line-1") are mutually inconsistent. |
| B2 | JDK fallback URL points at a non-existent build `jdk-17.0.19+10` | `tomcat_install.sh:103` | When Adoptium API is unreachable, fallback 404s → install fails offline. |
| B3 | `jsvc` compiled from source at install time, failures swallowed | `tomcat_install.sh:182–195` (`make 2>/dev/null … \|\| echo WARNING`) | On hosts without gcc/make, daemon mode silently degrades; no clear error surfaced to UI. |
| B4 | Hardcoded legacy JDK paths | `tomcat2_main.py:57–59` (`/usr/java/jdk1.7.0_80`, `/usr/java/jdk-11.0.2`) | Stale Java 7/11-era assumptions; not aligned to `/usr/local/btjdk`. |
| B5 | Tomcat port range capped at 8080–8084 | `tomcat2_main.py:92` (`range(8080, 8085)`) | Only 5 concurrent Tomcat sites; exhausts silently. |

---

## 5. Suspected risks (need runtime confirmation)

- **R1** — per-site full Tomcat `CATALINA_BASE` copy is heavy and duplicates config; upgrades must touch every site's `server.xml`.
- **R2** — `cp -r "$TC_PATH" "$BAK_PATH"` backup template (`tomcat_install.sh:202`) doubles disk per Tomcat; no disk-space precheck.
- **R3** — uninstall `rm -rf "$TC_PATH" …` (`:47`) does not verify the install is plugin-managed (could delete an externally-managed Tomcat sharing the path).

---

## 6. Security issues — see finding table (§L). Highlights:
- **No integrity validation** (SHA-512/GPG) on Tomcat or JDK downloads (`tomcat_install.sh:114,157–165`; Apache publishes `.sha512`/`.asc`, unused).
- **Command construction by string interpolation** throughout (`javaModel.py` 94×; `tomcat2_main.py:696,707,765,939`), incl. `domain` → `/etc/hosts` and `rm -rf`.
- **Predictable temp file** `/tmp/panelShell2.pl` (`javaModel.py:374`).

---

## 7. Tomcat 10/11 blockers
- **No `javax`→`jakarta` story** (0 references anywhere). Tomcat 10+ requires Jakarta EE 9+ (`jakarta.*`); WARs built for `javax.*` (Tomcat ≤9) **will not run** on 10/11 without migration. No detection, warning, or migration-tool integration exists. (Documentation + UX gap, not a code path.)
- **init.d-only** service model is not the aaPanel-modern or systemd-native expectation; no systemd unit generation.
- **No manager/host-manager/examples/docs removal or hardening** post-extract; stock webapps remain.
- Version pins (`10.1.55`, `11.0.22`, JDK `17.0.19`) are **static and partly wrong** (B2); need a freshness/resolver strategy.

## 8. Java 17/21 blockers
- JDK auto-install only fetches **17**; **21** is resolved only if already present (`tomcat_install.sh:68`), never installed.
- `java -version` parsing via `sed 's/.*version "\([0-9]*\).*/\1/'` (`:128`) is fragile across vendor banners.
- No removal of JVM flags unsupported on 17/21 (e.g. CMS/`-XX:+UseConcMarkSweepGC`, `-XX:MaxPermSize`) when reusing legacy app options.

## 9. aaPanel compatibility gaps
- Reuses aaPanel's plugin **id `10000`** and **name `tomcat2`** (`info.json`) → collides/overrides the official plugin instead of coexisting under its own identity.
- Depends on internal panel paths/symlinks (`/usr/local/bttomcat`, `/www/server/tomcat{N}`) and `public.ExecShell` — fine for an aaPanel plugin, but undocumented as a contract.

## 10. Third-party plugin conflict risks
- Edits `/etc/hosts` directly (`tomcat2_main.py:696,707`) — global side effect other plugins/admins don't expect.
- Claims ports 8080–8084 without cross-plugin reservation; no firewall coordination.
- Shared `/usr/local/btjdk` JDK could be changed under other Java consumers without warning.

---

## L. Finding table

| ID | Severity | Area | Finding | Evidence | Impact | Reproduction / Verification | Recommended Fix | Patch Complexity |
|----|----------|------|---------|----------|--------|------------------------------|-----------------|------------------|
| F1 | **Critical** | Legal | Public repo is an 88% verbatim fork of proprietary `tomcat2`; no LICENSE | `tomcat2_main.py` 1478/1674 == aaPanel src; `README.md:3`; `info.json name:tomcat2`; `gh` licenseInfo null; `license.txt §2.2,§4.3` | License breach; takedown/legal exposure | `diff` OSS `tomcat2_main.py` vs `aaPanel/graphify-in/plugin_tomcat2_main.py` → 178 changed lines only | Private repo now; clean-room rewrite; new plugin identity; add LICENSE | XL |
| F2 | **Critical** | Security/Supply-chain | No checksum/GPG verification of Tomcat & JDK tarballs | `tomcat_install.sh:114,157–165`; no `sha512/gpg` (grep empty) | MITM / mirror compromise → RCE as root at install | Run installer behind proxy serving altered tarball | Download Apache `.sha512` + KEYS `.asc`, verify before extract; pin + verify JDK | M |
| F3 | **High** | Security | Shell commands built via `%`/`+` interpolation incl. `domain` into `/etc/hosts` and `rm -rf` | `tomcat2_main.py:696,707,765,939`; `javaModel.py` 94× (e.g. `722,727`) | Command injection / unintended file ops if `domain`/version unvalidated | Create site with crafted domain containing shell metachars | Use `subprocess` arg lists / strict input validation + shlex.quote; never interpolate user data into shell | L |
| F4 | **High** | Reliability | init.d `JAVA_HOME` extracted from shebang line, not the inserted line | `tomcat_install.sh:177` vs `:215` | Tomcat starts with wrong/empty JAVA_HOME or fails | Install, then `cat /etc/init.d/tomcat11`; observe `JAVA_HOME=#!/bin/bash` | Write `JAVA_HOME` into a sourced env file; stop parsing `daemon.sh` | S |
| F5 | **High** | Tomcat 10/11 | No `javax`→`jakarta` detection/guidance | grep `javax\|jakarta` = 0 | javax-era WARs silently fail on T10/11 | Deploy a Servlet-3.x WAR to Tomcat 11 | Detect namespace; warn; document Tomcat migration tool; per-app target-version selector | M |
| F6 | **Medium** | Reliability | JDK fallback URL is a non-existent build | `tomcat_install.sh:103` | Offline/air-gapped install fails | Block api.adoptium.net, run installer | Resolve via API with retries; allow local JDK path; verified pinned fallback | S |
| F7 | **Medium** | Security | Predictable world-readable temp path | `javaModel.py:374` `/tmp/panelShell2.pl` | Symlink/race on shared host | `ls -l /tmp/panelShell2.pl` during op | `tempfile.mkstemp` with 0600 (already done correctly in `tomcat_install.sh` via `$$`) | S |
| F8 | **Medium** | Coexistence | Direct `/etc/hosts` edits & uncoordinated port claims | `tomcat2_main.py:696,707,92` | Conflicts with other plugins/admin; stale entries | Add 2 sites, inspect `/etc/hosts` | Use plugin-owned resolution; central port registry; cleanup on remove | M |
| F9 | **Medium** | Hardening | Stock manager/host-manager/examples/docs left in place; no AJP/shutdown-port hardening | `tomcat_install.sh` (no webapp removal); T10/11 ship them | Exposed mgmt apps / attack surface | `ls /www/server/tomcat11/webapps` post-install | Remove examples/docs; gate manager behind strong auth+localhost; set random shutdown cmd or `-1` | M |
| F10 | **Medium** | Java 17/21 | Only JDK 17 auto-installed; 21 never fetched; fragile version parse | `tomcat_install.sh:68,128` | Java 21 baseline unmet on clean host | Run on host with no JDK, request 21 | Add JDK 21 install path; robust `-version` parse (`-XshowSettings:properties`) | S |
| F11 | **Low** | Packaging | Build artifacts committed (`graphify-out/`, `.graphify_*`, `.zip`) | repo `find` listing | Repo bloat; leaks tool internals | `git ls-files \| grep graphify` | `.gitignore` + `git rm --cached` | XS |
| F12 | **Low** | Reliability | No disk-space precheck before double-copy backup template | `tomcat_install.sh:202` | Install fails mid-way on small disks | Fill disk near limit, install | `df` precheck; skip/replace backup template strategy | S |
| F13 | **Info** | Maintainability | Per-site full CATALINA_BASE copy + ElementTree server.xml edits (legacy from `class/tomcat.py` 2015) | `tomcat2_main.py:450–1058`; `class/tomcat.py` | Heavy, error-prone upgrades | n/a | Move to shared CATALINA_HOME + per-app CATALINA_BASE with templated configs | L |
| F14 | **Info** | Coexistence | Uninstall `rm -rf` of `/www/server/tomcat{N}` without managed-marker check | `tomcat_install.sh:47` | Could delete externally-managed Tomcat | n/a | Write+check a `.managed-by-plugin` marker before removal | S |
