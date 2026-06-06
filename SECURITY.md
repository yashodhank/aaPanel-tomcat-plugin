# Security Policy

JavaHost manages downloaded runtimes and long-running services on a server, so
security is a first-class concern. This document describes the hardening defaults
the plugin enforces, how to report a vulnerability, and which versions receive
fixes.

## Hardening defaults

These are enforced by the code, not optional configuration.

### Verified, fail-closed downloads
Every Tomcat and JDK artifact is verified before use (`core/util/download.py`):

- **SHA-512 is mandatory.** An artifact with no usable hash (explicit or a
  downloadable `.sha512`) is refused — `no SHA-512 provided … refusing to use
  unverified artifact`. A mismatch is a hard error.
- **OpenPGP signatures** are verified against the Apache KEYS keyring when
  signatures are published. If `gpg` is unavailable the signature step is skipped,
  but SHA-512 is still required — verification is never silently disabled.
- An **offline path** (local tarball + expected SHA-512) keeps integrity intact
  for air-gapped installs. The shell CLI (`tomcat_install.sh`) contains no
  download logic of its own and delegates to the verified Python installer, so
  verification cannot be bypassed.

### No shell string interpolation
Commands are always built as **argument lists** and executed without a shell
(`core/util/shell.py`). `shell=True` is forbidden; `run()` rejects a string
argument outright. Privilege drops use `sudo -n -u <user>` with a
previously-validated identifier. This removes shell-injection as a class of bug.

### Strict input validation
Every value from the panel request passes through `core/util/validate.py` before
it touches the filesystem, a command, or a template, and validators fail closed
(raise on bad input): identifiers, RFC-1123 domains, Tomcat majors (9/10/11),
ports (1–65535), Java majors (8/11/17/21), and memory bounds.

### Zip-slip-safe extraction
WAR/zip extraction validates that every entry resolves **inside** the target
directory; absolute paths, `..` traversal, and symlink entries are rejected
(`core/deploy/war.py`).

### Tomcat hardening on install
Applied to each freshly extracted CATALINA_HOME (`core/tomcat/hardening.py`):

- The `examples`, `docs`, `host-manager`, **and `manager`** webapps are removed
  by default; enabling `manager` is a deliberate, gated action.
- **AJP** is checked for: an active (uncommented) `AJP/1.3` connector is rejected.
- Services run as the unprivileged **`www`** user/group, not root.
- Config files are locked down (`conf/` → `0640`; `tomcat-users.xml` → `0600`).

### Service & secret handling
- Service units are generated for **systemd** (init.d fallback);
  `JAVA_HOME` is supplied via the environment, never parsed from a shebang
  (`core/tomcat/service.py`).
- Database credentials are written to a secret-safe `app.env` (mode `0640`)
  loaded via the unit's `EnvironmentFile`; credentials never appear in the
  connection URL, the WAR, process listings, or logs
  (`docs/databases-java-apps.md`). Lifecycle logging explicitly does not echo
  secrets.
- File writes are atomic (temp + rename) with explicit modes; temp dirs are
  `0700` with unpredictable names (`core/util/fs.py`).

### Guarded removal
Destructive operations refuse to delete anything outside the managed roots
(`/www/server/javahost`, plus JavaHost's own units under `/etc/systemd/system`
and `/etc/init.d`) or any directory lacking the `.javahost-managed` marker
(`core/util/fs.py`). Tomcat shutdown-port behaviour and bundled-webapp exposure
are minimized as above.

## Supported versions

JavaHost is pre-1.0. Security fixes target the latest released version on the
`main` line. Older point releases are not separately patched — upgrade to the
latest release.

| Version | Supported |
|---------|-----------|
| latest `0.1.x` (current `main`) | Yes |
| anything older | No — please upgrade |

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

- Report privately via GitHub Security Advisories
  ("Report a vulnerability") on the repository:
  <https://github.com/yashodhank/aaPanel-tomcat-plugin/security/advisories/new>
- Include affected version, environment (OS, panel, Java/Tomcat versions), a
  description, and reproduction steps or a proof of concept. Scrub any real
  credentials from your report.

We aim to acknowledge reports promptly, work with you on a fix and coordinated
disclosure, and credit reporters who wish to be named. Please give us reasonable
time to release a fix before any public disclosure.
