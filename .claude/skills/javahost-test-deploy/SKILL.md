---
name: javahost-test-deploy
description: >
  Generate sample WAR/JAR fixtures and run the JavaHost service-less deploy +
  database matrix on demand. Use when you need to validate real deployment
  behavior (WAR deploy, javax->jakarta detection/migration, executable JARs, and
  JDBC/Mongo connectivity) against a real Tomcat install — locally or on a panel
  host. NO Maven/Gradle: fixtures use stdlib zipfile + javac only.
allowed-tools: Bash(python3 tests/fixtures/make_samples.py *) Bash(python3 tests/e2e/deploy_matrix.py *) Bash(make samples) Bash(make test-deploy) Bash(python3 -m pytest tests/test_fixtures.py *) Bash(docker *)
---

# JavaHost — deploy & DB test runbook

Two tools, both stdlib-only (no build tool):
- `tests/fixtures/make_samples.py` — builds sample artifacts with `zipfile` + `javac`.
- `tests/e2e/deploy_matrix.py` — service-less deploy matrix (reuses the
  `smoke_noservice.py` pattern: drives `catalina.sh` / `java -jar` directly as the
  run user, no systemd), with an optional ephemeral Docker database.

## 1. Generate fixtures

```bash
make samples                                   # everything (app.jar skipped if no javac)
python3 tests/fixtures/make_samples.py --all
python3 tests/fixtures/make_samples.py --out /tmp/jhfix --db postgresql
```

Outputs into `tests/fixtures/out/` (gitignored):

| artifact      | what it is | marker / property |
|---------------|------------|-------------------|
| `hello.war`   | JSP + Jakarta EE 6.0 `web.xml` | serves `JAVAHOST_OK` (Tomcat compiles the JSP) |
| `legacy.war`  | same JSP, OLD `javax` schema + `WEB-INF/classes/javax/servlet/` marker | `war.detect_namespace()=='javax'` |
| `app.jar`     | `com.sun.net.httpserver` app, reads `SERVER_PORT` (default 8080) | runnable jar, serves `JAVAHOST_OK`; **needs javac** (skipped with a warning otherwise) |
| `boot.jar`    | Manifest `Main-Class=org.springframework.boot.loader.JarLauncher` + `BOOT-INF/` | `jar.detect_springboot()==True` (detection-only, not runnable) |
| `dbcheck.war` | `--db <engine>`: JSP reading `DB_URL`/`DB_USER`/`DB_PASSWORD` | prints `DB_OK` / `DB_FAIL:<reason>` |

`dbcheck.war` downloads the engine's recommended JDBC driver (coords from
`core.db.engines` -> Maven Central) into `WEB-INF/lib/`. Download is best-effort:
offline => warning, war still built (will `DB_FAIL` with ClassNotFound at runtime).
MongoDB is non-JDBC, so its dbcheck does a TCP-connect probe instead.

## 2. Offline pytest (no Tomcat, no network)

```bash
python3 -m pytest -q tests/test_fixtures.py
```
Asserts the WAR/boot.jar properties above; skips the app.jar check if javac is
absent.

## 3. Deploy matrix (real Tomcat, service-less)

Run on a panel host (or any box with the plugin importable + a Tomcat install).
It installs Tomcat via the plugin installer if missing.

```bash
make samples                 # build fixtures first
make test-deploy             # or: python3 tests/e2e/deploy_matrix.py
python3 tests/e2e/deploy_matrix.py --with-db postgresql
```

Steps (each asserts its marker; teardown always runs; non-zero exit on any fail):
1. install Tomcat (`JAVAHOST_E2E_TOMCAT`, default 11) if needed
2. create instance + deploy `hello.war` -> health for `JAVAHOST_OK`
3. `detect_namespace(legacy.war)=='javax'` (+ `war.migrate` -> `jakarta` if java present)
4. run `app.jar` (`java -jar` with `SERVER_PORT`) -> health for `JAVAHOST_OK`
   (skipped if app.jar was never generated)
5. `--with-db`: ephemeral Docker DB -> render engine env + write `app.env` ->
   deploy `dbcheck.war` -> assert `DB_OK` -> `docker rm -f`

Env knobs:
- `JAVAHOST_PLUGIN_DIR` — plugin dir on `sys.path` (default `/www/server/panel/plugin/javahost`)
- `JAVAHOST_E2E_TOMCAT` — Tomcat major (default `11`)
- `JAVAHOST_E2E_USER`   — run user; `root`/empty => direct mode (no `su`; for hosts that jail `su`)
- `JAVAHOST_FIXTURES`   — fixtures dir (default `tests/fixtures/out`)

## Docker DB recipes (known password `javahost`, db `jhtest`)

| engine     | image          | key env |
|------------|----------------|---------|
| postgresql | `postgres:17`  | `POSTGRES_PASSWORD=javahost POSTGRES_DB=jhtest` (user `postgres`) |
| mysql      | `mysql:8.4`    | `MYSQL_ROOT_PASSWORD=javahost MYSQL_DATABASE=jhtest` (user `root`) |
| mariadb    | `mariadb:11`   | `MARIADB_ROOT_PASSWORD=javahost MARIADB_DATABASE=jhtest` (user `root`) |
| mongodb    | `mongo:8`      | `MONGO_INITDB_ROOT_USERNAME=root MONGO_INITDB_ROOT_PASSWORD=javahost` |

The matrix maps each container's port onto a free loopback host port and waits
for TCP readiness (plus a grace period for server init) before deploying.

## 4. Full compatibility matrix (real host, cartesian sweep)

`deploy_matrix.py` (section 3) is the quick path. For the **complete** campaign —
every Tomcat line × every eligible Java major × every DB engine, plus the
JAR×Java×DB leg — use `tests/e2e/matrix_full.py`. Full guide:
[`docs/testbed.md`](../../../docs/testbed.md).

```bash
make samples                 # WAR/JAR fixtures
make samples-db              # add dbcheck.war + dbapp.jar (DB probes)
make matrix                  # full cartesian sweep, defaults
python3 tests/e2e/matrix_full.py --dry-run        # print the plan, run nothing
python3 tests/e2e/matrix_full.py --db-source docker
python3 tests/e2e/matrix_full.py --db-source aapanel --proxy
```

Sweeps **Tomcat 9 / 10.1 / 11 × eligible Java × DB{none,postgresql,mysql,mariadb,
mongodb}** + JAR×Java×DB. Eligible Java is gated by the per-line floor:

| Tomcat | namespace | eligible Java |
|--------|-----------|---------------|
| 9      | `javax`   | 8, 11, 17, 21 |
| 10.1   | `jakarta` | 11, 17, 21    |
| 11     | `jakarta` | 17, 21        |

Ineligible cells (e.g. Tomcat 11 / Java 11) are **skipped**, not failed.

Flags:
- `--db-source aapanel|docker` — both supported. `aapanel`: panel-managed DBs
  (install + create db/user via the App Store). `docker`: ephemeral throwaway
  containers (same recipes as section "Docker DB recipes", `docker rm -f` on exit).
- `--proxy` — also create a reverse-proxy site per app and assert the **real
  hostname** `<app>.5d.bisotech.in`, not just loopback.
- `--dry-run` — print the planned cells and exit (run this first to check scope).

**Bytecode pinning to prove Java binding:** `make samples`/`make_samples.py` take
`--release N` to compile a fixture to a specific bytecode level. A `--release 21`
artifact only runs on Java 21+, so `JAVAHOST_OK` on a Tomcat-9/Java-21 cell
*proves* the app actually bound to Java 21 (not a lower default JDK).

**Service path:** the runner **prefers the systemd path** (`javahost-<app>`
units); on a hardened host where it cannot register services it **falls back to a
service-less run** automatically. For an unattended full sweep, prefer hardening
**OFF** (re-enable at teardown) — see `docs/testbed.md` and `docs/system-hardening.md`.

## 5. Reverse-proxy / domains (`*.5d.bisotech.in`)

Tomcat/JAR connectors bind to **`127.0.0.1:<port>` by design** — apps are **NOT**
reachable on their raw public port. You reach them through a reverse-proxy domain.
DNS `*.5d.bisotech.in` (and apex `5d.bisotech.in`) already points to the box.

- **`SetSite{app, domain?}`** — create a reverse-proxy site → app's loopback port.
  No `domain` ⇒ `<app>.<suffix>`, where the suffix is the plugin config key
  **`site_suffix`** (`/www/server/javahost/config.json`), **empty by default** —
  so with no suffix set you MUST pass `domain` (no FQDN is guessed). The campaign
  box has `site_suffix: "5d.bisotech.in"`, hence `<app>.5d.bisotech.in`. Uses the
  aaPanel site API, falls back to writing an nginx vhost. `--proxy` does this and
  asserts the hostname for you.
- **`RemoveSite{app}`** — remove that site (teardown).

Verify: `curl http://<app>.5d.bisotech.in/ | grep JAVAHOST_OK` (off-box) or
`curl http://127.0.0.1:<port>/` (on-box). Never expect the app on `public-ip:port`.

### Per-site HTTPS (`SetSiteSSL`)

- **`SetSiteSSL{app, enable, email?}`** — `enable` truthy issues a Let's Encrypt
  cert and flips the vhost to HTTPS (`:443` terminates TLS, `:80` serves the ACME
  challenge + 301-redirects); falsy reverts to plain HTTP. The domain is the
  site's stored domain / `?domain=` / the `site_suffix` convention — never guessed.
- **Issuance: aaPanel-native first, certbot `--webroot` fallback** (both serve the
  HTTP-01 challenge from a shared webroot, so no downtime).
- **Auto-renewal** via a certbot deploy hook
  (`/etc/letsencrypt/renewal-hooks/deploy/javahost-nginx.sh`) that reloads nginx.
- **State marker** `<base>/bin/site.ssl` (read by `list_apps()`); the `:443` vhost
  uses the LE live cert at `/etc/letsencrypt/live/<domain>/`.
- **Cert kept on disable** — re-enabling is instant (no re-issue).

With SSL on, `request scheme` reads `https` end-to-end (proxy sets
`X-Forwarded-Proto https`) — visible in the `hello.war` "served by" block. Verify:
`curl -s https://<app>.5d.bisotech.in/ | grep JAVAHOST_OK`.

## 6. Tasks & Logs (observability)

Long ops (Install Java/Tomcat) run as **background jobs** —
`StartInstallJava` / `StartInstallTomcat` / `StartUninstallTomcat` return a
`{job_id}` immediately, so slow downloads **do not "false-error"** the request.

- **Tasks** UI section: job status `running`/`done`/`failed` + elapsed + view-log.
  Endpoints: `GetJobs` (list), `GetJobLog` (one job). Only treat an install as
  failed when its task shows **failed**.
- **Logs** UI section: **app logs** (per-app Catalina/JAR) + **task logs**
  (per-job output). Look for `JAVAHOST_OK`/`DB_OK` in app logs, download/verify
  lines in task logs.

## Expected markers
- App served OK: response body contains `JAVAHOST_OK`
- DB reachable: response body contains `DB_OK`; failures read `DB_FAIL:<reason>`
- Namespace: `detect_namespace` returns `javax` (legacy) / `jakarta` (migrated)
- Spring Boot: `detect_springboot` returns `True` for `boot.jar`
- Java binding: a `--release N`-pinned fixture serving `JAVAHOST_OK` proves Java ≥ N

## Cleanup
Both tools self-clean: instances are removed via `fs.safe_rmtree`, JAR processes
terminated, Docker containers `docker rm -f`'d in a `finally`. To wipe fixtures:
`rm -rf tests/fixtures/out`. If a run was killed mid-flight, remove leftover
containers: `docker rm -f javahost-e2e-postgresql javahost-e2e-mysql javahost-e2e-mariadb javahost-e2e-mongodb`.
