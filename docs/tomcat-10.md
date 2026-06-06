# Tomcat 10.1

JavaHost models Tomcat 10 as the `10.1` line. The rules live in
`core/tomcat/registry.py`:

```
"10": TomcatLine(major="10", line="10.1", min_java=11, namespace="jakarta", legacy=False)
```

## Key specifics

- **Minimum Java: 11.** `ensure_java("10")` resolves an installed JDK `>= 11`;
  if none exists it auto-installs Temurin 17 (the lowest modern JDK at/above
  the floor).
- **Namespace: `jakarta`.** Tomcat 10.1 implements Jakarta EE 9+, so the
  Servlet/JSP APIs moved from `javax.*` to `jakarta.*`.
- **Not legacy** (`legacy=False`), unlike the Tomcat 9 line.

## Version resolution

`resolve_latest_patch("10")` reads the live Apache index at
`https://dlcdn.apache.org/tomcat/tomcat-10/` and selects the newest `10.1.Z`
href. If the index can't be read it falls back to the conservative pin
`_FALLBACK_PATCH["10"]` (currently `10.1.55`). Integrity is still enforced at
install regardless of which path produced the version.

## Install, paths, verification

`installer.install("10")` resolves the artifact, then:

1. Enforces the Java floor first (`ensure_java`), before anything is written.
2. Verified download via `download.fetch_verified`: SHA-512 (from the artifact's
   `.sha512`) is mandatory, and OpenPGP signature (`.asc`) is checked against a
   keyring built from `tomcat-10` Apache KEYS when `gpg` is available. An
   unverifiable artifact is an error, never a silent skip. An offline path
   (local tarball + expected hash) is supported.
3. Staged extract into `tomcat/10.staging` (`tar -xzf --strip-components=1`).
4. Harden + write `.javahost-version` marker + mark managed.
5. Atomic swap into `/www/server/javahost/tomcat/10` (old install moved aside
   to `.old` and removed only on success). Failures discard staging and leave
   any existing install intact.

Shared CATALINA_HOME: `/www/server/javahost/tomcat/10`. Per-app CATALINA_BASE:
`/www/server/javahost/instances/<app>`.

## Hardening (`hardening.py`)

Applied to the freshly extracted home and each instance base:

- Removes bundled `examples`, `docs`, `host-manager`, and `manager` webapps by
  default. `manager` is only kept via an explicit `keep_manager` opt-in.
- `assert_no_ajp` strips XML comments first, then fails if an **active**
  `AJP/1.3` connector exists. The rendered `server.xml` ships no AJP connector
  and binds the HTTP connector to `127.0.0.1` only (TLS terminates at the
  panel-managed Nginx reverse proxy); `server` shutdown port is `-1`/`DISABLED`.
- `secure_perms` locks `conf/` to `0640` and `tomcat-users.xml` to `0600`.

WAR deploys are scanned: deploying a `javax.*`-namespace WAR onto Tomcat 10
returns a UI warning that it requires the `jakarta.*` namespace and will not
run as-is (use the Apache Tomcat Migration Tool, or deploy on Tomcat 9).

## What differs from Tomcat 9

| | Tomcat 9 | Tomcat 10.1 |
|---|---|---|
| line | `9.0` | `10.1` |
| min Java | 8 | 11 |
| namespace | `javax` | `jakarta` |
| legacy flag | `True` | `False` |

The namespace change is the breaking one: apps written for `javax.servlet`
(Jakarta EE 8 / Java EE) do not run on Tomcat 10 without migration.
