# JavaHost — on-box test campaign (the full matrix)

This is the canonical guide for running a **complete compatibility sweep** of
JavaHost on a real aaPanel/BaoTa host: every supported Tomcat line × every
eligible Java major × every database engine, plus the executable-JAR paths. It
is the heavyweight counterpart to the manual [Testing runbook](testing.md) and
the [deploy & DB test skill](../.claude/skills/javahost-test-deploy/SKILL.md).

Use it when you want to *prove* — not spot-check — that the plugin installs
runtimes, deploys apps, binds each app to the Java major you asked for, connects
to each DB engine, and is reachable through a real reverse-proxy hostname.

Two success markers prove a deploy is healthy end-to-end:

- **`JAVAHOST_OK`** — emitted by the sample apps once they are serving requests.
- **`DB_OK`** — emitted by `dbcheck.war` / `dbapp.jar` only after reading
  `app.env`, loading the JDBC driver (or running the Mongo probe), and
  round-tripping a query against the configured DB.

---

## The #1 gotcha — apps bind to loopback, you reach them via a domain

**Read this before you "verify" anything.**

Every Tomcat connector and every JAR HTTP server JavaHost manages binds to
**`127.0.0.1:<port>` by design.** This is deliberate: the raw application port is
**not** exposed on the box's public interface, so an app is **not reachable on
its public IP:port**. Hitting `http://<public-ip>:8080/` will time out or refuse
— that is expected, not a failure.

You reach an app **through a reverse-proxy domain**. JavaHost can create that
reverse-proxy site for you (see [Reverse proxy & domains](#reverse-proxy--domains)
below); requests arrive at the panel's web server on `:80/:443` and are proxied
to the app's loopback port. So:

- **Health/`JAVAHOST_OK` from on the box** → curl `http://127.0.0.1:<port>/`.
- **Health/`JAVAHOST_OK` from anywhere** → curl `http://<app>.5d.bisotech.in/`.
- **Never** expect the app on `http://<public-ip>:<port>/`.

---

## Prerequisites

### 1. Hardening must be OFF for the campaign

A full sweep **creates systemd services**, **installs language runtimes (JDKs)**,
**installs Tomcat tarballs**, and (with the aaPanel DB source) **installs DB
servers**. Service registration is the part that fights aaPanel **System
Hardening**. For an unattended campaign — especially the systemd path that
`make matrix` prefers — turn hardening **off** for the duration, then turn it
back on.

JavaHost *can* run under hardening (it briefly lifts/re-locks the immutable bit;
see [System Hardening](system-hardening.md)), and `matrix_full.py` will **fall
back to a service-less path on hardened hosts**. But for the canonical campaign:

- Toggle per [System Hardening](system-hardening.md): aaPanel → **Security /
  System Hardening (系统加固)** → turn **off**, run the campaign, turn back **on**.
  Fully reversible.
- Equivalently set `manage_hardening: true` (the default) in
  `/www/server/javahost/config.json` so the plugin handles the immutable bit
  itself — but the cleanest full-matrix run is with hardening disabled and
  re-enabled at teardown.
- If a global **LD_PRELOAD exec filter** (bt_security / usranalyse) is active,
  systemd services will be blocked (`status=203/EXEC`). Either authorize JavaHost
  (run `AllowServices`, then approve in **Security → bt_security**) or run the
  matrix **service-less** (it detects this and falls back automatically).

### 2. DNS is already wired

`*.5d.bisotech.in` (wildcard) and the apex `5d.bisotech.in` already resolve to
this box's public IP. You do **not** need to create DNS records — only the
reverse-proxy **site** that maps a hostname to an app's loopback port. The
convention is one hostname per app:

```
<app>.5d.bisotech.in   ->   box IP   ->   nginx proxy   ->   127.0.0.1:<app-port>
5d.bisotech.in         ->   box IP   ->   nginx proxy   ->   (apex / default app)
```

### 3. Databases: aaPanel-managed vs Docker

The matrix can get its databases two ways, chosen with `--db-source`:

- **`aapanel`** — use databases installed and managed through the aaPanel UI
  (App Store → install PostgreSQL / MySQL / MariaDB / MongoDB, create a db +
  user). Closest to how a real operator runs production DBs on the box.
- **`docker`** — stand up ephemeral, throwaway DB containers (the same recipes
  the deploy skill uses; known password `javahost`, db `jhtest`). Fastest for a
  clean sweep; nothing persists. Requires Docker on the host.

Both are fully supported. Pick `aapanel` to validate the real panel-managed DB
story, or `docker` for a fast, reproducible cartesian run.

---

## Compatibility matrix

JavaHost enforces a **minimum Java floor per Tomcat line**. The campaign only
runs Tomcat×Java cells that are *eligible*; ineligible cells (e.g. Tomcat 11 on
Java 11) are skipped, not failed.

| Tomcat line | Namespace | Min Java | Eligible Java majors |
|-------------|-----------|----------|----------------------|
| 9           | `javax`   | 8        | 8, 11, 17, 21        |
| 10.1        | `jakarta` | 11       | 11, 17, 21           |
| 11          | `jakarta` | 17       | 17, 21               |

Database engines swept for each eligible cell:

| Engine | Default port | Driver coordinates | Probe |
|--------|--------------|--------------------|-------|
| *(none)*   | —     | —                                | app-only (`JAVAHOST_OK`) |
| PostgreSQL | 5432  | `org.postgresql:postgresql`      | JDBC round-trip → `DB_OK` |
| MySQL      | 3306  | `com.mysql:mysql-connector-j`    | JDBC round-trip → `DB_OK` |
| MariaDB    | 3306  | `org.mariadb.jdbc:mariadb-java-client` | JDBC round-trip → `DB_OK` |
| MongoDB    | 27017 | `org.mongodb:mongodb-driver-sync` | TCP/driver probe → `DB_OK` |

The sweep is the cartesian product **Tomcat × eligible-Java × DB{none,pg,mysql,
mariadb,mongo}** using the WAR fixtures, plus an analogous **JAR × Java × DB**
leg using the executable-JAR fixtures. The Java floors come from the same source
as [Tomcat 10.1](tomcat-10.md) / [Tomcat 11](tomcat-11.md); the DB matrix matches
[Connecting Java apps to databases](databases-java-apps.md).

---

## Step-by-step campaign

### Step 1 — Build the fixtures

From the repository root, build the sample artifacts (WARs + JARs). Add the DB
probe variants too:

```bash
make samples        # hello/legacy/app/boot WARs + JARs
make samples-db     # adds dbcheck.war + dbapp.jar (JDBC/Mongo probes)
```

Outputs land in `tests/fixtures/out/`:

| Artifact | Shape | What it proves |
|----------|-------|----------------|
| `hello.war`   | `jakarta.*` WAR | baseline WAR deploy → `JAVAHOST_OK` |
| `legacy.war`  | `javax.*` WAR | namespace warning + `javax`→`jakarta` migrate |
| `app.war`     | minimal app WAR | a second clean deploy target |
| `boot.war`    | Boot-style WAR | servlet-container Boot path |
| `dbcheck.war` | WAR + JDBC/Mongo probe | reads `app.env`, connects → `DB_OK` |
| `dbapp.jar`   | executable JAR + DB probe | service-less JAR + DB → `DB_OK` |

**Pinning bytecode to prove Java binding:** pass `--release N` so a fixture is
compiled to a specific bytecode level. An artifact built with `--release 21` will
only run on Java 21+, so a successful `JAVAHOST_OK` on a Tomcat-9/Java-21 cell
*proves* the app actually bound to Java 21 — not to some lower default JDK on the
box. The matrix uses this to assert real per-app Java selection.

### Step 2 — Install runtimes & Tomcats (watch them in **Tasks**)

In the UI (**Runtimes** tab) or via the matrix runner, install the Java majors
and Tomcat lines you intend to sweep.

> **Async jobs — installs no longer "false-error" on slow downloads.** Installing
> a JDK or Tomcat is now a **background job**: the UI calls
> `StartInstallJava` / `StartInstallTomcat` (and `StartUninstallTomcat`) and gets
> back a **`{job_id}`** immediately instead of blocking. A slow Adoptium/Apache
> download will **not** time the request out and surface a spurious error.
> Watch progress in the new **Tasks** section (status `running` / `done` /
> `failed`, elapsed time, and **view-log**); the backend exposes `GetJobs` for
> the list and `GetJobLog` for a single job's log. Only treat an install as
> failed when its task shows **failed** — not because the click "took a while".

Install, at minimum, every Java major and Tomcat line from the
[matrix](#compatibility-matrix) above, then confirm each shows **done** in Tasks.

### Step 3 — Install / select databases

Pick your DB source:

- **`--db-source aapanel`**: install PostgreSQL / MySQL / MariaDB / MongoDB from
  the aaPanel App Store, create a database + user for each, and note the
  host/port/credentials. The matrix writes these into each app's `app.env` via
  `SetDbEnv`.
- **`--db-source docker`**: nothing to pre-install — the matrix spins up ephemeral
  containers per engine and tears them down in a `finally`.

JavaHost itself never administers DB servers; it only writes the secret-safe
`app.env` each app reads (see [databases-java-apps.md](databases-java-apps.md)).

### Step 4 — Run the full matrix

```bash
make matrix                                   # full cartesian sweep, defaults
python3 tests/e2e/matrix_full.py --dry-run    # print the plan, run nothing
python3 tests/e2e/matrix_full.py --db-source docker
python3 tests/e2e/matrix_full.py --db-source aapanel --proxy
```

`tests/e2e/matrix_full.py` sweeps **Tomcat 9 / 10.1 / 11 × eligible Java
(9: 8–21, 10.1: 11–21, 11: 17–21) × DB {none, postgresql, mysql, mariadb,
mongodb}**, plus the **JAR × Java × DB** leg. Each cell deploys the fixture,
starts the app, and asserts its marker.

Flags:

- `--db-source aapanel|docker` — where databases come from (both supported).
- `--proxy` — also create the reverse-proxy site per app and assert reachability
  via the **real hostname** `<app>.5d.bisotech.in`, not just loopback.
- `--dry-run` — print the planned cells (the cartesian plan) and exit without
  installing or deploying anything. Run this first to sanity-check scope.

**Service path:** the runner **prefers the systemd path** (real
`javahost-<app>` units). On a **hardened host** where it cannot register
services, it **falls back to a service-less run** (drives `catalina.sh` /
`java -jar` directly as the run user) so the matrix still completes.

### Step 5 — Verify via the reverse-proxy domain

For each app, the canonical check is the **hostname**, not the port (remember the
[loopback invariant](#the-1-gotcha--apps-bind-to-loopback-you-reach-them-via-a-domain)):

```bash
curl -s http://hello.5d.bisotech.in/      | grep JAVAHOST_OK
curl -s http://dbcheck.5d.bisotech.in/    | grep DB_OK
```

With `--proxy`, the matrix does these hostname asserts for you. Without it, you
verify on the box with `curl http://127.0.0.1:<port>/`.

### Step 6 — Read **Logs** and **Tasks**

The UI gained two observability sections that make a campaign legible:

- **Tasks** — every background job (install / uninstall) with status, elapsed
  time, and **view-log**. This is where you confirm a runtime/Tomcat install
  actually finished (`done`) vs. is still `running` vs. `failed`.
- **Logs** — both **app logs** (per-app Catalina / JAR stdout) and **task logs**
  (the per-job output behind each Task). Look for `JAVAHOST_OK` / `DB_OK` in app
  logs and for download/verify lines in task logs.

If a cell fails, open its app log (marker missing? namespace warning? driver
`ClassNotFound`?) and the relevant task log (download/verify/extract).

### Step 7 — Teardown

- Delete the apps the matrix created (`DeleteApp` per app removes instance, files,
  and the `javahost-<app>` service) and remove their proxy sites
  (`RemoveSite{app}`).
- Optionally uninstall the Tomcat lines (`StartUninstallTomcat` → watch in Tasks).
- If you used `--db-source docker`, containers are `docker rm -f`'d automatically;
  for `aapanel` DBs, drop the test databases/users from the panel if desired.
- Remove fixtures: `rm -rf tests/fixtures/out`.
- **Re-enable System Hardening** (and any exec filter you disabled) — see
  [System Hardening](system-hardening.md).

---

## Reverse proxy & domains

JavaHost can create the reverse-proxy site that fronts an app's loopback port,
following the convention `<app>.5d.bisotech.in` (with the apex `5d.bisotech.in`
for a default/apex app). Endpoints:

- **`SetSite{app, domain?}`** — create a reverse-proxy site for `app`. With no
  `domain`, it uses `<app>.5d.bisotech.in`; pass `domain` to override. It targets
  the app's **loopback** port (`127.0.0.1:<port>`) and is created through the
  **aaPanel site API**, falling back to writing an **nginx vhost** directly if
  the site API is unavailable.
- **`RemoveSite{app}`** — remove that reverse-proxy site.

This is what makes an app reachable at all from outside the box — see the
[loopback invariant](#the-1-gotcha--apps-bind-to-loopback-you-reach-them-via-a-domain).
For the manual include-snippet alternative, see the **Reverse-proxy hint** card
in the [User Guide](user-guide.md#6-reverse-proxy).

---

## Expected markers (quick reference)

- **App served OK** — response body contains `JAVAHOST_OK`.
- **DB reachable** — response body contains `DB_OK`; failures read
  `DB_FAIL:<reason>`.
- **Java binding** — a `--release N`-pinned fixture serving `JAVAHOST_OK` proves
  the app bound to Java ≥ N.
- **Namespace** — `legacy.war` raises a namespace warning on a `jakarta` line and
  serves cleanly after **Migrate & deploy**.
