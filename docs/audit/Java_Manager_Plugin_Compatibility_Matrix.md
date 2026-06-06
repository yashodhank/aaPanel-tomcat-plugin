# Java Manager — Plugin Compatibility Matrix

**Date:** 2026-06-06
**Legend:** ✅ supported/works · ⚠️ partial/with caveats · ❌ not supported/blocked · 🆕 new in OSS target · 🔬 needs runtime test on clean host.

"Existing Plugin" = current `aaPanel-tomcat-plugin` fork (≈ aaPanel `tomcat2` + the new `tomcat_install.sh`).
"New OSS Plugin Target" = clean-room `javahost` design.

---

## 1. Feature matrix

| Feature | Existing Plugin | New OSS Plugin Target | Java 17 | Java 21 | Tomcat 10 | Tomcat 11 | Ubuntu 24.04 | Notes |
|---------|-----------------|-----------------------|---------|---------|-----------|-----------|--------------|-------|
| Legally publishable | ❌ (fork of proprietary) | ✅ (clean-room, §3.1) | — | — | — | — | — | F1 — gating |
| Distinct plugin identity | ❌ reuses `tomcat2`/id 10000 | 🆕 `javahost`/new id | — | — | — | — | — | enables coexistence |
| Tomcat 7/8 | ⚠️ legacy paths only | ❌ out of scope | — | — | — | — | ⚠️ | EOL; drop |
| Tomcat 9 (legacy) | ✅ | ⚠️ isolated legacy flag | ✅ | ✅ | — | — | ✅ | javax.*; warn EOL |
| Tomcat 10.1 | ⚠️ installs, init.d | ✅ primary, systemd+hardened | ✅ | ✅ | ✅ | — | 🔬 | min Java 11 |
| Tomcat 11.0 | ⚠️ installs, init.d | ✅ modern, Java-17 gate | ✅ | ✅ | — | ✅ | 🔬 | min Java 17 |
| Download integrity (sha512+gpg) | ❌ none (F2) | 🆕 mandatory, fail-closed | ✅ | ✅ | ✅ | ✅ | ✅ | supply-chain |
| Java 8 detect | ✅ | ✅ | — | — | ⚠️(T10 only via 11) | ❌ | ✅ | T11 needs 17+ |
| Java 11 detect/install | ⚠️ detect only | ✅ detect (+install opt) | — | — | ✅ | ❌ | ✅ | T10 floor |
| Java 17 detect/install | ⚠️ install via fallback (B2) | 🆕 robust install+verify | ✅ | — | ✅ | ✅ | 🔬 | primary baseline |
| Java 21 detect/install | ❌ detect-only (F10) | 🆕 install+verify | — | ✅ | ✅ | ✅ | 🔬 | modern baseline |
| Per-runtime JAVA_HOME | ⚠️ via daemon.sh parse (B1) | 🆕 systemd `Environment=` | ✅ | ✅ | ✅ | ✅ | ✅ | fixes F4 |
| JVM flag validation (17/21) | ❌ (F10) | 🆕 strip unsupported flags | ✅ | ✅ | ✅ | ✅ | ✅ | CMS/PermSize removed |
| Service mgmt | ⚠️ init.d/chkconfig | 🆕 systemd (+init.d fallback) | ✅ | ✅ | ✅ | ✅ | ✅ | reboot-safe |
| javax→jakarta handling | ❌ (F5) | 🆕 detect+warn+migrate tool | — | — | ✅ | ✅ | ✅ | app-level |
| WAR deploy | ⚠️ (panel-side) | ✅ zip-slip-safe | ✅ | ✅ | ✅ | ✅ | ✅ | security |
| Exploded app deploy | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Manager app default | ⚠️ stock present (F9) | 🆕 disabled by default | — | — | ✅ | ✅ | ✅ | hardening |
| examples/docs removed | ❌ (F9) | 🆕 removed on install | — | — | ✅ | ✅ | ✅ | attack surface |
| AJP connector | ⚠️ stock (off) not asserted | 🆕 omitted+asserted off | — | — | ✅ | ✅ | ✅ | |
| Shutdown port | ⚠️ stock 8005 | 🆕 disabled (-1) | — | — | ✅ | ✅ | ✅ | |
| Runs as non-root (`www`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Port conflict detection | ⚠️ 8080–8084 only (B5) | 🆕 registry+socket check | — | — | ✅ | ✅ | ✅ | coexistence |
| Nginx reverse proxy | ⚠️ panel-side | ✅ plugin-owned vhost | ✅ | ✅ | ✅ | ✅ | 🔬 | no foreign edits |
| `/etc/hosts` edits | ❌ writes directly (F8) | 🆕 none | — | — | ✅ | ✅ | ✅ | coexistence |
| PostgreSQL 17 JDBC guidance | ❌ | 🆕 template+env-file | ✅ | ✅ | ✅ | ✅ | ✅ | guidance only |
| Secret-safe (no creds in logs/WAR) | ⚠️ | 🆕 env-file 0640, no echo | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Idempotent install/reinstall | ⚠️ version.pl guard | ✅ atomic staging | ✅ | ✅ | ✅ | ✅ | 🔬 | |
| Rollback on failed install | ⚠️ partial | 🆕 staging discard+restore | ✅ | ✅ | ✅ | ✅ | 🔬 | |
| Managed-marker uninstall | ❌ blind rm -rf (F14) | 🆕 marker-gated | — | — | ✅ | ✅ | ✅ | |
| Disk-space precheck | ❌ (F12) | 🆕 `df` gate | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Health check | ❌ | 🆕 poll w/ timeout | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Logs viewer | ✅ (catalina) | ✅ + rotation | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Offline/manual install | ⚠️ partial (B2 fallback broken) | 🆕 local tarball+sha | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Original UI | ❌ aaPanel layout (F1/§5) | 🆕 clean-room | — | — | — | — | — | |
| CI / signed release | ✅ CI (no signing) | ✅ CI + signed | — | — | — | — | — | |

---

## 2. OS support matrix (target)

| OS | Existing | New target | Notes |
|----|----------|-----------|-------|
| Ubuntu 22.04 | ⚠️ untested | ✅ 🔬 | systemd present |
| Ubuntu 24.04 | ⚠️ untested | ✅ 🔬 (primary) | systemd; default test bed |
| Debian 11/12 | ⚠️ | ✅ 🔬 | practical |
| CentOS/RHEL/Rocky/Alma | ⚠️ chkconfig path | ⚠️ best-effort | only if systemd + glibc OK; init.d fallback |
| Containers w/o systemd | ❌ | ⚠️ init.d fallback | document limitation |

---

## 3. Third-party plugin coexistence

| Plugin | Risk (existing) | New-target stance |
|--------|-----------------|-------------------|
| Nginx manager | edits/relies on panel Nginx | generate **plugin-owned** vhost only; backup-before-write shared includes; `nginx -t` before reload |
| Apache manager | port overlap | port registry + socket check; never claim used ports |
| PostgreSQL plugin | none direct | guidance-only; connect over localhost; no schema changes |
| Firewall/security | silent port use | never modify firewall silently; surface "open port X?" to user |
| SSL cert plugin | TLS termination | terminate TLS at Nginx (plugin vhost), not Tomcat |
| File/Process/Cron mgr | shared FS/procs | plugin-owned paths; systemd units namespaced `javahost-*` |
| Backup plugins | config rewrite | backup config before rewrite; managed-marker |
| Monitoring | none | expose health endpoint + log paths |
| Docker/Coolify-like | port/Java overlap | detect external Tomcat/Java; mark external vs managed; never delete external |

---

## 4. What must be runtime-verified (🔬) on a clean Ubuntu 24.04

1. Tomcat 10.1 + 11.0 download → **sha512/gpg verify** → extract → start as `www`.
2. systemd unit `systemctl enable --now`; survives reboot; correct JAVA_HOME.
3. Java 17 **and** 21 install from Adoptium with verification; T11 refused on Java < 17.
4. WAR deploy + Nginx vhost + health poll; javax-WAR warning on T11.
5. Idempotent reinstall; failed-install rollback; managed-marker uninstall leaves external Tomcat intact.
