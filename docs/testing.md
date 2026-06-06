# JavaHost — manual testing runbook

A task-oriented checklist for validating a JavaHost install end-to-end on a real
aaPanel/BaoTa host. It maps each step to the plugin's UI sections and the
server-side endpoints they call (see [User Guide](user-guide.md) for screenshots
and [Connecting Java apps to databases](databases-java-apps.md) for the DB
reference). Allow ~30 minutes for a full pass.

Two success markers prove a deploy is healthy end-to-end:

- **`JAVAHOST_OK`** — emitted by the sample apps once they are serving requests.
- **`DB_OK`** — emitted by `dbcheck.war` only after it has read `app.env`,
  loaded the JDBC driver, and round-tripped a query against the configured DB.

---

## 0. Generate sample artifacts

The sample apps are built offline from the in-repo fixtures generator. From the
repository root:

```bash
make samples
```

This runs `tests/fixtures/make_samples.py` and writes five artifacts to
`tests/fixtures/out/`:

| Artifact | Shape | What it exercises |
|----------|-------|-------------------|
| `hello.war` | `jakarta.*` WAR | baseline WAR deploy on Tomcat 10/11; prints `JAVAHOST_OK` |
| `legacy.war` | `javax.*` WAR | namespace warning + the `javax`→`jakarta` migrate path |
| `app.jar` | executable fat JAR | service-less JAR app; reads a Spring-style profile |
| `boot.jar` | Spring Boot fat JAR | Spring Boot auto-detection + profile pass-through |
| `dbcheck.war` | WAR with a JDBC probe | reads `app.env`, connects, prints `DB_OK` |

Copy these to a path the panel file API can reach (e.g. your home dir on the
host) so the UI upload dialogs can pick them up.

---

## 1. Runtimes — install Java + Tomcat

Open the **Dashboard** first; it calls `GetStatus` and should render without the
red hardening banner on a default host. Then open **Runtimes**.

1. **Install Java 17** — click **Install** on the Java 17 row (`InstallJava`).
   Wait for the *installed* badge. Repeat for **Java 21** if you want to test the
   Tomcat 11 floor with the higher JDK.
2. **Install Tomcat 10.1** — click **Install** on the 10.1 row (`InstallTomcat`).
   Confirm the row shows a resolved patch, namespace `jakarta`, and min Java 11.
3. **Install Tomcat 11** — click **Install** on the 11 row (`InstallTomcat`).
   Confirm min Java 17 is enforced (if no qualifying JDK existed, the server
   should have auto-installed one).

Expected: Dashboard now shows both Java and Tomcat majors with patch levels.

### Tasks & Logs (observability)

Long operations like **Install Java** and **Install Tomcat** now run as
**background jobs**: the UI calls `StartInstallJava` / `StartInstallTomcat` (and
`StartUninstallTomcat`) and gets a **`{job_id}`** back immediately instead of
blocking. A slow Adoptium/Apache download therefore **no longer "false-errors"**
the request — watch it finish in the UI instead of assuming it failed.

- **Tasks** section — lists each job with status (`running` / `done` / `failed`),
  elapsed time, and **view-log**. Backed by `GetJobs` (list) and `GetJobLog`
  (one job's output). Only treat an install as failed when its task shows
  **failed**.
- **Logs** section — shows both **app logs** (per-app Catalina / JAR output) and
  **task logs** (the per-job output). Look for `JAVAHOST_OK` / `DB_OK` in app
  logs and download/verify lines in task logs.

---

## 2. Applications — create + deploy `hello.war`

Open the **Applications** tab.

1. **Create app** → fill **App name** (e.g. `hello`), pick **Tomcat 11**, leave
   **Port** `8080` and **Memory** `512`. Submit (`CreateApp`). The app appears
   with a `javahost-hello` service.
2. **Deploy WAR** → select the `hello` app, choose `hello.war`, click
   **Deploy WAR** (`UploadWar`). The file uploads via the panel file API, stages
   under `/tmp`, and extracts zip-slip-safely into `webapps/ROOT`. No namespace
   warning is expected (`hello.war` is `jakarta.*`).
3. **Start / Restart** the app via the row action (`AppAction`).

> **Loopback / reverse-proxy gotcha.** Tomcat (and JAR) connectors bind to
> **`127.0.0.1:<port>` by design** — the app is **not** reachable on the box's
> raw public port. Verify on the box with `http://127.0.0.1:<port>/`; to reach it
> from anywhere, create a reverse-proxy site (`SetSite{app, domain?}`, convention
> `<app>.5d.bisotech.in`) and hit the hostname. See
> [Test campaign](testbed.md#the-1-gotcha--apps-bind-to-loopback-you-reach-them-via-a-domain).

### Verify health, logs, metrics

- **Check health** → the row health pill flips to `up` with the port and HTTP
  code (`GetHealth`). Hitting `http://127.0.0.1:8080/` should return the
  `JAVAHOST_OK` body.
- **View logs** → open the log viewer (`GetLogs`), set lines to 200, look for the
  startup line and `JAVAHOST_OK`.
- **Metrics** → open the metrics panel (`GetMetrics`); confirm a PID, RSS MB,
  thread count, and uptime are reported from `/proc`.

---

## 3. Legacy WAR — namespace warning + migrate

Still in **Applications**, using the same `hello` app (or a fresh `legacy` app on
Tomcat 11):

1. **Deploy WAR** → choose `legacy.war`, click **Deploy WAR** (`UploadWar`).
   Because `legacy.war` is `javax.*` on a `jakarta` Tomcat line, the response
   should surface a **namespace warning** in the returned `warning` field / toast.
2. Now choose **Migrate & deploy** (`MigrateWar`) with `legacy.war`. The server
   runs the Apache `javax`→`jakarta` migration tool, then deploys the converted
   artifact into `webapps/ROOT`.
3. Restart and re-check health/logs — the migrated app should now serve
   `JAVAHOST_OK` cleanly with no warning.

---

## 4. Spring Boot / executable JAR with a profile

From **Applications** click **Deploy JAR**.

1. **App name** `boot`, **Java major** `17`, a free **Port**, **Memory** `512`.
2. **Spring profiles**: enter `prod` (or `prod,metrics`).
3. Choose `boot.jar` (or `app.jar` for the plain executable-JAR path) and submit
   (`CreateJarApp`). Spring Boot is auto-detected; the profile is passed through
   to the running service.
4. **Check health / View logs** — confirm the app is `up`, prints `JAVAHOST_OK`,
   and that the active profile you entered appears in the startup log.

---

## 5. Databases — `SetDbEnv` + `dbcheck.war` → `DB_OK`

Open the **Databases** tab. The top card is the read-only support matrix
(`GetDbSupport`): PostgreSQL, MySQL, MariaDB, MongoDB with default ports and
recommended drivers.

1. Create a target app (e.g. `dbcheck` on Tomcat 11) under **Applications** if
   you do not already have one.
2. In **Databases**, pick the `dbcheck` app in the **Per-app database
   environment** picker (or the row's **Database env** action).
3. Choose the **engine** (e.g. `postgresql`); the version list and **port**
   auto-fill and the recommended **driver** is shown. Enter **host**
   (`127.0.0.1`), **database**, **user**, **password**.
4. Click **Write DB env** (`SetDbEnv`). This writes
   `CATALINA_BASE/bin/app.env` (mode `0640`); the UI confirms the engine and
   driver but **never echoes the secrets**.
5. Make sure the JDBC driver coordinates from the support matrix are available to
   the app (drop the driver in `CATALINA_HOME/lib` or bundle per your build).
6. **Deploy WAR** → `dbcheck.war` to the `dbcheck` app (`UploadWar`), restart,
   then **View logs** / hit the app. A healthy round-trip prints **`DB_OK`**.
   Repeat for MySQL / MariaDB / MongoDB to cover each engine.

If you see a connection error instead of `DB_OK`, confirm the DB is reachable
from the host and that `app.env` host/port/credentials match the running server.

---

## 6. Reverse proxy

Open the **Help** tab → **Reverse-proxy hint** card (`GetProxyHint`). Copy the
generated Nginx **include snippet** (it targets a local upstream like
`127.0.0.1:<port>`) into the site's Nginx config to publish a managed app on a
domain. JavaHost owns only its own vhost and never edits other plugins' configs.

---

## 7. Cleanup

For each app created above, use the row's **More actions → Delete app**
(`DeleteApp`); confirm the prompt. This removes the instance, its files, and the
`javahost-<app>` service. Optionally uninstall Tomcat majors (`UninstallTomcat`)
and remove the generated artifacts under `tests/fixtures/out/`.

---

## Automated equivalent

The same deploy paths run unattended via the in-repo end-to-end harness:

```bash
make samples       # tests/fixtures/make_samples.py --all
make test-deploy   # tests/e2e/deploy_matrix.py
```

`make test-deploy` exercises the WAR/JAR/migrate matrix service-less (no DB). The
opt-in CI workflow `.github/workflows/deploy-matrix.yml` runs the same harness on
`workflow_dispatch` and on a weekly schedule, including a `--with-db` matrix that
stands up PostgreSQL, MySQL, MariaDB, and MongoDB as service containers and
asserts the `DB_OK` marker. It never runs on push.

### Full compatibility sweep on a box

For the **complete** Tomcat × eligible-Java × DB cartesian campaign on a real
host (`make matrix` / `tests/e2e/matrix_full.py`, the `--db-source` choice,
`--proxy` real-hostname asserts, and the `--release N` bytecode pinning that
proves per-app Java binding), see the dedicated
[Test campaign](testbed.md) guide.
