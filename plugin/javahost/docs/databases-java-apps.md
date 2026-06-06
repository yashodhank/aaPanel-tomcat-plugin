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

## API

- `GetDbSupport` → every engine, its version list, recommended driver, and any
  locally-detected server.
- `SetDbEnv` with `db_engine` (postgresql|mysql|mariadb|mongodb), `db_host`,
  `db_port` (optional → engine default), `db_name`, `db_user`, `db_password`,
  and optional `db_version` → writes `app.env`.
