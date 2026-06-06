# Connecting Java apps to databases

JavaHost is a Tomcat/Java runtime manager, **not** a database manager. It does
not install or administer database servers — it helps your Java app *connect* to
one safely: it builds the correct connection URL, recommends the right driver
for your JVM, and writes credentials to a secret-safe env file.

## Supported engines & versions

| Engine | Server versions | Driver class | Driver (Java 8+) | Default port |
|--------|-----------------|--------------|------------------|--------------|
| PostgreSQL | 9.4 – 18 | `org.postgresql.Driver` | `org.postgresql:postgresql:42.7.4` | 5432 |
| MySQL | 5.5 – 9.x | `com.mysql.cj.jdbc.Driver` | `com.mysql:mysql-connector-j:9.1.0` | 3306 |
| MariaDB | 10.2 – 11.x | `org.mariadb.jdbc.Driver` | `org.mariadb.jdbc:mariadb-java-client:3.5.1` | 3306 |
| MongoDB | 3.6 – 8.0 | `com.mongodb.client.MongoClients` | `org.mongodb:mongodb-driver-sync:5.2.1` | 27017 |

A single modern driver per engine connects to every listed server version; for
ancient JVMs (Java 6/7) the plugin recommends the matching legacy driver.

## Secret-safe pattern (all engines)

JavaHost writes `CATALINA_BASE/bin/app.env` (mode `0640`) and the systemd unit
loads it via `EnvironmentFile`. Your app reads:

```
DB_URL           # connection URL/URI (no password embedded)
DB_USER
DB_PASSWORD      # supply to the driver/credential at runtime
DB_DRIVER        # driver class
DB_DRIVER_MAVEN  # coordinates to drop into your build / CATALINA_HOME/lib
```

- **Never** hardcode credentials in the WAR or source.
- Credentials never appear in process listings, the connection URL, or logs.

### Example URLs produced

```
PostgreSQL : jdbc:postgresql://db:5432/appdb?sslmode=require
MySQL      : jdbc:mysql://db:3306/appdb?sslMode=REQUIRED
MariaDB    : jdbc:mariadb://db:3306/appdb?sslMode=REQUIRED
MongoDB    : mongodb://db:27017/appdb?tls=true      (user/password supplied via MongoCredential)
```

## In the Databases tab

- The top card is a read-only **support matrix** (`GetDbSupport`): every engine,
  its version range, default port, recommended driver, and whether the engine is
  detected running locally.
- Below it, the per-app **database env** section has a live **search/filter**
  (with a live count) over the per-app env chips, so you can find an app quickly
  on a busy host.
- Picking an app opens the **Configure database env** form: engine, host, port,
  database, user, password, and an **SSL** checkbox. SSL **defaults off for
  loopback hosts** (`127.0.0.1`/`localhost`/`::1`) and on for remote — so a local
  DB connects out of the box. Submitting calls `SetDbEnv`; the UI then offers to
  restart the app.
- The drawer's **Database** tab shows the **current** connection env
  (`GetDbEnv`): engine, the connection URL (host/port/db — **never** the
  password), user, driver, and whether a password is set — or "No database env
  configured".

## SSL / `db_ssl`

`SetDbEnv` honours an explicit `db_ssl` flag; with none it defaults SSL **off**
for loopback hosts and **on** for remote. The rendered URL reflects this (e.g.
`?sslmode=require` for PostgreSQL, `?sslMode=REQUIRED` for MySQL/MariaDB,
`?tls=true` for Mongo).

## API

- `GetDbSupport` → every engine, its version list, recommended driver, and any
  locally-detected server.
- `SetDbEnv` with `db_engine` (postgresql|mysql|mariadb|mongodb), `db_host`,
  `db_port` (optional → engine default), `db_name`, `db_user`, `db_password`,
  optional `db_version`, and optional `db_ssl` → writes `app.env` (secrets are
  never echoed back).
- `GetDbEnv{app}` → the current env, **secret-safe**: `{configured, engine, url,
  user, driver, driver_maven, has_password}`. It returns whether a password is
  set, **never** the password itself.
