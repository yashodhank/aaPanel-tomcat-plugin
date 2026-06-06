# Troubleshooting

All issues below are grounded in JavaHost's actual behaviour. Operations return
the panel's standard `{status, msg}` envelope; on failure `msg` carries the
error string. Most failures are intentionally **fail-closed** — JavaHost refuses
to proceed rather than do something unsafe.

## Download or verification failures

JavaHost verifies every Tomcat/JDK artifact before using it, and never silently
skips verification (`core/util/download.py`).

- **`no SHA-512 provided for <name>; refusing to use unverified artifact`**
  No hash was available (neither explicit nor a downloadable `.sha512`). For an
  offline install you must pass `--sha512`. See INSTALL.md (offline section).
- **`SHA-512 mismatch for <name>`**
  The downloaded (or local) file does not match the expected hash. Re-download,
  or confirm the `--sha512` you passed matches the tarball. A mismatch is always
  treated as an error.
- **`gpg not installed; cannot verify OpenPGP signature`**
  Signature verification was requested but `gpg` is missing. Install `gpg`, or
  proceed with SHA-512 only — when no keyring can be built the installer skips
  the signature step but still enforces SHA-512 (`installer._keyring` returns
  `None` if gpg/keys are unavailable).
- **OpenPGP `--verify` failed**
  The detached signature did not validate against the Apache KEYS keyring. Do not
  bypass this; re-fetch the artifact from an official mirror.
- **Downloads hang/time out**
  `curl` is used with retries and `--max-time`; if curl is absent, a `urllib`
  fallback is used. Check outbound HTTPS to Apache/Adoptium, or use the offline
  `--local` path.

## Java version floor not met

Each Tomcat line has a minimum Java major that is enforced before any files are
written (`installer.ensure_java`):

- Tomcat 10.1 requires **Java 11+**.
- Tomcat 11 requires **Java 17+**.

If no installed JDK satisfies the floor, JavaHost auto-installs a verified
Temurin JDK (17, or 21 when the floor is above 17). To pin which detected JDK is
used, pass `--prefer-java N` (still subject to the floor). Supported Java majors
are 8 / 11 / 17 / 21; anything else is rejected by input validation.

## systemd vs init.d

JavaHost prefers systemd and falls back to init.d automatically
(`core/tomcat/service.py`). systemd is used only when **both** `systemctl` is on
PATH **and** `/run/systemd/system` exists.

- With systemd: units are `javahost-<app>.service`; lifecycle uses
  `systemctl enable --now / start / stop / restart`, status via `is-active`.
- Without systemd: a lint-clean script is written to `/etc/init.d/javahost-<app>`
  and driven with `start|stop|restart|status`.

Both paths consume the same `bin/setenv.sh` as the single source of truth.
`JAVA_HOME` is supplied via the environment, never parsed from a shebang.

If a service status reads `unknown` (systemd) or `absent`/`inactive` (init.d),
inspect the per-app logs (below), then try **RepairApp**.

## Port already in use

App ports are validated to the 1–65535 range, but JavaHost does not pre-check
whether a port is free. If Tomcat fails to bind, the per-app `catalina` log will
show a bind/`Address already in use` error. Pick a free port when creating the
app (`CreateApp` accepts `port`, default 8080), or stop the conflicting service.
The instance's listening port is read back from `conf/server.xml`
(`GetAppDetail`).

## javax → jakarta WAR warning

Tomcat 10/11 use the `jakarta.*` namespace; Tomcat 9 uses `javax.*`. On deploy,
JavaHost inspects the WAR and returns a non-fatal `warning` (`DeployWar` →
`war.namespace_warning`) when there is a mismatch:

- A `javax`/mixed WAR on Tomcat 10/11: it will not run as-is — use the Apache
  Tomcat Migration Tool for Jakarta EE, or deploy on Tomcat 9.
- A `jakarta` WAR on Tomcat 9: it requires Tomcat 10+.

The WAR is still extracted (extraction is zip-slip-safe); the warning surfaces in
the response so the UI can show it.

## Where logs live

- **Per-app logs:** `/www/server/javahost/instances/<app>/logs/`. JavaHost reads
  `catalina.out` first, then any `catalina*.log` (`instance.tail_log`).
- **GetLogs endpoint:** returns a memory-safe tail of that log. `lines` defaults
  to 200 and is clamped to 2000.
- **Panel action log:** lifecycle actions are recorded via the panel's
  `WriteLog` under the `JavaHost` tag. Secrets are never logged — `SetDbEnv`
  reports `env: written (secrets not echoed)`.

## Repairing a broken app (RepairApp)

Use **RepairApp** after an OS upgrade or when a unit is stale/half-broken
(`instance.repair`). It:

1. Reads `JAVA_HOME` / `CATALINA_HOME` back from the app's `bin/setenv.sh`
   (errors if either is missing).
2. Removes a stale `temp/tomcat.pid`.
3. Re-renders and reinstalls the service unit.
4. Restarts it if active, otherwise enables + starts it.

If `setenv.sh` is missing the required values, repair fails with a clear message
— recreate the app in that case.

## "Refusing to remove …" on uninstall/delete

Removal helpers (`fs.safe_rmtree`) refuse to delete a directory that lacks the
`.javahost-managed` marker or sits outside the managed roots
(`/www/server/javahost`, and JavaHost's own units under `/etc/systemd/system` /
`/etc/init.d`). This is a guardrail, not a bug: only JavaHost-created paths are
removable. Plugin uninstall keeps runtimes/apps unless you set `PURGE=1`.
