---
name: javahost-security
description: >
  Security review checklist for the JavaHost plugin's specific attack surface —
  command execution, verified downloads, archive extraction, systemd/init.d units,
  credential handling, Tomcat hardening, and input validation. Use when reviewing a
  diff for security, before a release, or when adding any code that runs commands,
  downloads files, extracts archives, writes services, or handles secrets.
---

# JavaHost security review checklist

Distilled from SAST practice (utkusen/sast-skills, Bandit/Semgrep categories) and
scoped to *this* plugin's surface. Instructions-only — no external scanners are run
by this skill. If `shellcheck`/`bandit` happen to be installed, you may run them, but
do not add them as hard dependencies.

## Review gates (every change)
1. **Command injection** — all OS commands go through `core.util.shell.run([...])`
   arg-lists. Fail the review on any `shell=True`, `os.system`, `os.popen`, or
   f-string/`%`/`+` building a command line. User data must never reach a shell verbatim.
2. **Input validation** — every `get.*` value is validated via `core.util.validate`
   (identifier/domain/port/version/memory) before touching fs, shell, templates, or URLs.
3. **Download integrity** — new downloads use `util.download.fetch_verified`
   (SHA-512 required; GPG when keyring present; fail-closed). No silent skip, no HTTP.
4. **Archive extraction** — WAR/zip via `deploy.war.safe_extract` only: reject `..`,
   absolute paths, and symlink entries (zip-slip). No raw `extractall`.
5. **Filesystem safety** — writes via `fs.atomic_write` with explicit mode; deletions
   via `fs.safe_rmtree` (refuses unmanaged dirs / paths outside MANAGED_ROOTS).
   Temp dirs via `fs.mkdtemp` (0700) — no predictable `/tmp/*.pl` paths.
6. **Secrets** — DB credentials only in `app.env` (0640), never in the connection
   URL/URI, never logged, never echoed back in an endpoint response. `GetDbEnv`
   returns `has_password` only — never the password. `tomcat-users.xml` 0600.
7. **Service units** — `JAVA_HOME` comes from `Environment=`/setenv, never parsed
   from a shebang. Units run as `www` (non-root); `NoNewPrivileges`/`PrivateTmp` set.
   **init.d must NOT `. app.env`** (shell-sourcing re-evaluates `$(...)`/backticks
   in a value → a DB password could execute as root); load vars line-by-line.
8. **Tomcat hardening** — installer removes examples/docs/host-manager/manager;
   `hardening.assert_no_ajp` passes (no *active* AJP); HTTP connector bound to 127.0.0.1;
   shutdown port disabled. TLS terminates at the Nginx vhost, not Tomcat. **JAR apps
   also bind loopback** (`SERVER_ADDRESS`/`SERVER_HOST=127.0.0.1`) — never `0.0.0.0`.
9. **Coexistence** — never edit `/etc/hosts` or another plugin's configs; only write
   plugin-owned paths; check ports before claiming them; never delete external Tomcats.
   The Danger-zone wipe must SKIP in-use runtimes and never touch the panel JDK/cert,
   other plugins, or databases.
10. **SSL / certs** — `SetSiteSSL` issues against a **real** domain only (stored
    site / explicit / `site_suffix` convention) — never a guessed FQDN; certbot
    errors are surfaced, not swallowed. No vendor FQDN is hardcoded (`site_suffix`
    is config, empty by default).
11. **Async jobs** — `core.jobs` work bodies are built from validated inputs only;
    never interpolate raw `get.*` into the job's python/argv. Jobs are pruned.
12. **Least privilege / authorization** — destructive actions are marker-gated and
    logged; the typed `WIPE` confirm guards `Wipe`; no production testing without
    authorization.

## Output
Produce a findings table: `Severity (Critical/High/Medium/Low/Info) | Area | Finding
| Evidence (file:line) | Fix`. Prefer fixing the class (e.g. route through the safe
helper) over patching one call site. Add/adjust a unit test for any security fix.

## Storage-profile secrets (multi-destination backups)
- Multiple S3 destinations ⇒ multiple secret keys, all in `0600`
  `/www/server/javahost/remotes.json`. A Get/List endpoint MUST NEVER return a
  `secret_key` — only a `secret_set` flag (mirror `GetDbEnv`). On update, an empty
  secret keeps the stored one.
- Backups contain the app DB env (`bin/app.env`) → archives `0600`; the backups dir
  is a managed root. LE private keys are NEVER bundled (re-issued on restore).
- Restore/upload is the untrusted-input path → only `archive.safe_extract_tar`
  (symlink/hardlink/device/`..`/absolute rejected). Cron exprs validated to `[0-9*/,-]`.
- Never inline a secret in a shell/ssh/docker command (process table leak) — read
  from the gitignored `_private_spec/OPS-ACCESS.md` or the on-box `remotes.json` at
  runtime. Treat any chat-pasted cloud key as exposed and require rotation.
