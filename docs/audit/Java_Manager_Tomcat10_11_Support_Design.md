# Java Manager — Tomcat 10 & 11 Support Design

**Date:** 2026-06-06
**Status:** design (clean-room target). Verify version pins against upstream at build time.

> **Hard rule:** Tomcat 11 is **not** a drop-in replacement for Tomcat 10. Differences below drive the version model.

---

## 1. Version model & compatibility facts

| Tomcat | Spec namespace | Servlet | Min Java | Notes / status |
|--------|----------------|---------|----------|----------------|
| 9.0.x | `javax.*` | 4.0 | Java 8 | **Legacy** path only; keep isolated. Approaching EOL — warn users. |
| 10.1.x | `jakarta.*` (Jakarta EE 9→10) | 6.0 | **Java 11+** | Primary modern LTS-style line. (10.0.x used Java 8 but is EOL — do **not** target 10.0.) |
| 11.0.x | `jakarta.*` (Jakarta EE 11) | 6.1 | **Java 17+** | Modern optional baseline. GA Oct 2024. |

**10 → 11 is not drop-in. Concrete differences to handle:**
1. **Java floor rises 11 → 17.** Refuse install/start of T11 on Java < 17 (already enforced in installer `MIN_JDK=17`; keep + surface in UI).
2. **Servlet 6.0 → 6.1**, some deprecated APIs removed; apps may compile-break.
3. **Namespace:** both 10 and 11 are `jakarta.*` — a `javax.*` (Tomcat ≤9) WAR runs on **neither** without migration. Provide the **Apache Tomcat Migration Tool for Jakarta EE** as an opt-in deploy step, and detect `javax.servlet` in WARs to warn.
4. **Config drift:** `server.xml`/`web.xml` are largely compatible 10↔11 but ship per-major templates rather than reuse one file across majors.

**Version registry (data, not code paths):**
```python
TOMCAT = {
  "9":  {"line":"9.0",  "min_java":8,  "namespace":"javax",   "legacy":True},
  "10": {"line":"10.1", "min_java":11, "namespace":"jakarta", "legacy":False},
  "11": {"line":"11.0", "min_java":17, "namespace":"jakarta", "legacy":False},
}
# exact patch (e.g. 10.1.x / 11.0.x) resolved at install time from Apache's
# download index, NOT hardcoded — fixes audit B2 (stale/wrong pins).
```

---

## 2. Install source strategy + integrity (fixes F2)

1. Resolve latest patch for the chosen line from `https://dlcdn.apache.org/tomcat/tomcat-<major>/` (parse index), fallback `https://archive.apache.org/...`.
2. Download `apache-tomcat-X.Y.Z.tar.gz` **and** `.tar.gz.sha512` **and** `.tar.gz.asc`.
3. **Verify SHA-512** (`sha512sum -c`) — mandatory, fail closed.
4. **Verify GPG** against Apache Tomcat `KEYS` (ship a pinned copy in the plugin; `gpg --verify`). If `gpg` absent, require SHA-512 + warn.
5. Only then extract. Same model for JDK (Adoptium publishes checksums + signatures).
6. **Offline mode:** accept a local `--tarball /path` + `--sha512 <hex>`; never silently skip verification.

---

## 3. Install paths & layout

```
/www/server/javahost/
├── runtimes/jdk-{8,11,17,21}/         # managed JDKs (per-runtime)
├── tomcat/{10,11,9}/                  # shared CATALINA_HOME per major (read-only-ish)
│   ├── conf/  bin/  lib/  ...
│   └── .javahost-managed              # ownership marker (fixes F14)
└── instances/<app>/                   # per-app CATALINA_BASE (lightweight, fixes F13/R1)
    ├── conf/{server.xml,context.xml,web.xml}   # rendered from templates
    ├── webapps/  logs/  work/  temp/
    └── bin/setenv.sh                  # JAVA_HOME, JAVA_OPTS, CATALINA_OPTS
```
- **Shared CATALINA_HOME + per-app CATALINA_BASE** replaces the legacy "full copy per site" model — upgrades patch one HOME, instances keep their own config.
- No more `/etc/hosts` edits (fixes F8); hostnames handled via the Nginx vhost + `Host` config only.

---

## 4. systemd unit strategy (replaces init.d; fixes F4)

Per app instance, generated unit `javahost-<app>.service`:
```ini
[Unit]
Description=JavaHost Tomcat instance <app>
After=network.target

[Service]
Type=forking
User=www
Group=www
Environment=JAVA_HOME=/www/server/javahost/runtimes/jdk-17
Environment=CATALINA_HOME=/www/server/javahost/tomcat/11
Environment=CATALINA_BASE=/www/server/javahost/instances/<app>
EnvironmentFile=-/www/server/javahost/instances/<app>/bin/app.env
ExecStart=/www/server/javahost/tomcat/11/bin/catalina.sh start
ExecStop=/www/server/javahost/tomcat/11/bin/catalina.sh stop
SuccessExitStatus=143
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```
- JAVA_HOME comes from `Environment=`/`EnvironmentFile=`, **never** parsed from `daemon.sh` (kills B1 class of bug).
- `Type=forking` + `catalina.sh` (drop fragile compiled-`jsvc` dependency, B3 — jsvc optional, not required).
- **Fallback:** if `systemctl` is absent (containers/old distros), emit a minimal, lint-clean init.d using the same env file — single source of truth.
- `EnvironmentFile` is generated with `0640 root:www`, values quoted/escaped (prevents env injection).

---

## 5. Config templates (Jinja, per major)

- `server.xml.j2`: single HTTP `Connector` on the app's port bound to `127.0.0.1` (Nginx fronts TLS); **AJP connector omitted** (fixes F9). `Server` `shutdown` port set to `-1` (disable) or a random command string.
- `context.xml.j2`, `web.xml.j2`: conservative defaults; `DefaultServlet` `readonly=true`, `listings=false`.
- `setenv.sh.j2`: memory (`-Xms/-Xmx` from UI), `-Djava.security.egd`, GC flags **validated for the target Java** (strip `UseConcMarkSweepGC`, `MaxPermSize`, etc. on 17/21 — fixes F10/jvm_opts).
- Rendering is unit-tested offline (no service start needed).

---

## 6. Security defaults (stricter than the old plugin) — fixes F9

On every install:
1. `rm -rf webapps/{examples,docs}` (and `host-manager` unless explicitly enabled).
2. `manager` app: **disabled by default**; if enabled, bind to `127.0.0.1`, require a generated strong password in `tomcat-users.xml` (`0600`), and a `RemoteAddrValve` allowlist.
3. Shutdown port disabled (`-1`).
4. Ensure **no AJP** connector active (Tomcat ships it disabled since 8.5.51/9.0.31; assert it stays off).
5. Run as **`www`** (non-root), files owned `www:www`, configs `0640`, secrets `0600`.
6. HTTP connector bound to localhost; public exposure only via the plugin-managed Nginx vhost.

---

## 7. App deployment flow

1. UI: choose app name → Tomcat major → JDK → port (auto from a tracked registry, fixes F5/B5) → memory.
2. Create instance (CATALINA_BASE), render templates, write systemd unit, `systemctl enable --now`.
3. Deploy WAR: **zip-slip-safe** extraction (validate each entry path is within target; reject `..`/absolute) — `deploy/war.py`.
4. **Namespace check:** scan WAR for `javax.servlet` on a T10/11 target → warn + offer migration tool.
5. Health check: poll `http://127.0.0.1:<port>/` (or configured probe) with timeout; report status in UI.
6. Reverse proxy: generate plugin-owned Nginx vhost → app port; never edit other plugins' configs (backup-before-write if touching shared include dir).

---

## 8. Rollback strategy

- **Install:** stage to `instances/<app>.staging` and `tomcat/<major>.staging`; atomically `mv` into place on success; on any failure delete staging, leave existing untouched. Disk precheck via `df` first (fixes F12).
- **Upgrade:** snapshot `conf/` + record current symlink target; on failed start (`systemctl is-active` poll), restore previous HOME symlink + conf, restart, surface error.
- **Uninstall:** only remove resources carrying `.javahost-managed`; never `rm -rf` an unmarked Tomcat (fixes F14). Leave shared JDKs unless `--purge-runtimes`.
- Every destructive op logs the exact resolved command + path to the plugin log before executing.
