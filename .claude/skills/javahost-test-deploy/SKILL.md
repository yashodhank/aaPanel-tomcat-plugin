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

## Expected markers
- App served OK: response body contains `JAVAHOST_OK`
- DB reachable: response body contains `DB_OK`; failures read `DB_FAIL:<reason>`
- Namespace: `detect_namespace` returns `javax` (legacy) / `jakarta` (migrated)
- Spring Boot: `detect_springboot` returns `True` for `boot.jar`

## Cleanup
Both tools self-clean: instances are removed via `fs.safe_rmtree`, JAR processes
terminated, Docker containers `docker rm -f`'d in a `finally`. To wipe fixtures:
`rm -rf tests/fixtures/out`. If a run was killed mid-flight, remove leftover
containers: `docker rm -f javahost-e2e-postgresql javahost-e2e-mysql javahost-e2e-mariadb javahost-e2e-mongodb`.
