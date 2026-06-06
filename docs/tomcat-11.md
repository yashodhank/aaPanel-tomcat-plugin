# Tomcat 11

JavaHost models Tomcat 11 as the `11.0` line. The rules live in
`core/tomcat/registry.py`:

```
"11": TomcatLine(major="11", line="11.0", min_java=17, namespace="jakarta", legacy=False)
```

## Key specifics

- **Minimum Java: 17 (enforced).** `ensure_java("11")` resolves an installed
  JDK `>= 17`; if none qualifies it auto-installs Temurin 17. The floor is
  enforced before any file is written by `installer.install`.
- **Namespace: `jakarta`.** Like 10.1, Tomcat 11 is Jakarta EE (`jakarta.*`),
  not `javax.*`.
- **Servlet 6.1.** Tomcat 11 implements Jakarta EE 10/11-era specs, including
  Servlet 6.1 (vs Servlet 6.0 on Tomcat 10.1).

## Version resolution

`resolve_latest_patch("11")` reads `https://dlcdn.apache.org/tomcat/tomcat-11/`
and picks the newest `11.0.Z` href, falling back to `_FALLBACK_PATCH["11"]`
(currently `11.0.22`) if the live index is unreadable. SHA-512 + OpenPGP
verification still apply on install either way.

## Install, paths, hardening

Identical orchestration to the other lines (`installer.install("11")`):
Java-floor check -> verified download (mandatory SHA-512, plus `.asc` against
`tomcat-11` KEYS when `gpg` is present) -> staged extract into
`tomcat/11.staging` -> harden -> `.javahost-version` marker -> atomic swap into
`/www/server/javahost/tomcat/11`, with discard-on-failure rollback.

Hardening is the same as 10.1: `examples`/`docs`/`host-manager`/`manager`
removed by default, no active AJP connector permitted, loopback-only HTTP
connector, `conf/` perms tightened. Per-app CATALINA_BASE lives at
`/www/server/javahost/instances/<app>` against the shared CATALINA_HOME at
`/www/server/javahost/tomcat/11`.

## Tomcat 11 is NOT a drop-in for 10

Despite both being `jakarta.*`, Tomcat 11 is not a transparent replacement for
Tomcat 10.1:

- **Java floor rises 11 -> 17.** Tomcat 10.1 runs on Java 11; Tomcat 11
  requires Java 17 as a hard minimum. JavaHost encodes and enforces this
  (`min_java=17`), so an app that ran on 10.1 under Java 11 must move to a
  JDK 17+ runtime before it can run on 11.
- **Removed/changed APIs.** Tomcat 11 drops deprecated APIs carried by 10.1 and
  raises spec baselines (Servlet 6.0 -> 6.1, and related Jakarta EE bumps).
  Apps relying on removed methods or older spec behavior need code/config
  changes.
- **Namespace.** Both lines are `jakarta.*`, so a `javax.*` (Jakarta EE 8) app
  still cannot run on 11 without the Jakarta migration — the same WAR
  `namespace_warning` that fires for Tomcat 10 fires for 11.

### Migration notes

- Coming from **Tomcat 9** (`javax`): first migrate to `jakarta.*` (Apache
  Tomcat Migration Tool for Jakarta EE), then ensure a JDK 17+ runtime. The WAR
  deploy path warns when a `javax`/mixed-namespace WAR targets 11.
- Coming from **Tomcat 10.1** (`jakarta`): namespace is already correct; raise
  the runtime to Java 17+ and recheck for removed APIs and Servlet 6.1
  behavior. JavaHost will auto-provision a qualifying JDK if none is present.
- After moving an existing instance, use `RepairApp` to re-render its service
  unit from `setenv.sh` and restart.
