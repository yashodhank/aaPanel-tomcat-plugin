# JavaHost — Tomcat & Java Runtime Manager for aaPanel/BaoTa

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

An **independent, open-source** aaPanel/BaoTa-style plugin to install and manage
**Apache Tomcat 9 / 10.1 / 11** and **Java 8 / 11 / 17 / 21**, with verified
downloads, systemd services, reverse-proxy and PostgreSQL helpers.

> **Why this exists:** the panel's built-in Java manager stalled at Tomcat 7/8/9.
> JavaHost adds modern Tomcat 10/11 + Java 17/21 the right way, as a clean,
> maintainable community plugin.

## Independence & licensing

JavaHost is a **clean-room** implementation. It is **not** a fork of aaPanel's
proprietary `tomcat2` plugin and contains **no aaPanel source code, UI, or
assets**. It interoperates only through the panel's public, documented
third-party plugin API (which the AAPANEL license §3.1 explicitly permits for
independently-developed plugins). Licensed under **Apache-2.0** — see
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Third-party runtimes (Apache Tomcat, Eclipse Temurin/OpenJDK, PostgreSQL JDBC)
are **downloaded and integrity-verified at runtime**, not bundled.

## Features

| Area | What you get |
|------|--------------|
| Tomcat | 9 (legacy), 10.1, 11 — dynamic latest-patch resolution, **SHA-512 + OpenPGP verified** downloads |
| Java | Detect 8/11/17/21; install Temurin 17/21 (verified); per-runtime `JAVA_HOME`; JVM-flag validation |
| Services | **systemd** units (init.d fallback); `JAVA_HOME` via env, never parsed from a shebang |
| Security | manager/examples/docs removed by default, AJP off, shutdown port disabled, runs as `www`, configs `0640`/secrets `0600` |
| Deploy | zip-slip-safe WAR extraction; `javax`→`jakarta` namespace detection & warnings for Tomcat 10/11 |
| Proxy | plugin-owned Nginx vhost generator (never edits other plugins' configs) |
| Databases | **PostgreSQL (9.4–18), MySQL (5.5–9.x), MariaDB (10.2–11.x), MongoDB (3.6–8.0)** — connection-URL builder, JVM→driver matrix, secret-safe `app.env` (no creds in WAR or logs) |
| Lifecycle | idempotent install, atomic staging + rollback, disk precheck, managed-marker uninstall |

## Compatibility

- Panels: aaPanel / BaoTa-style (Python 3 plugin runtime).
- OS: Ubuntu 22.04 / 24.04 (primary), Debian 11/12; EL (Rocky/Alma) best-effort.
- Tomcat 10.1 needs Java 11+, Tomcat 11 needs Java 17+ (enforced).

## Install

**ZIP import (recommended):** download `javahost.zip` from
[Releases](https://github.com/yashodhank/aaPanel-tomcat-plugin/releases) →
aaPanel → App Store → Third-party → Import Plugin.

**From source (your own panel):**
```bash
make deploy VPS_HOST=root@your-server   # rsync plugin/javahost + restart panel
```

## Repository layout

```
plugin/javahost/            # the deployable plugin (everything the panel needs)
├── info.json               # manifest (name: javahost)
├── javahost_main.py        # aaPanel entrypoint (thin glue)
├── index.html  icon.svg    # original UI + icon
├── install.sh  tomcat_install.sh
└── core/                   # portable library (no panel coupling except core/compat)
    ├── util/  runtime/  tomcat/  deploy/  db/  compat/
tests/                      # offline unit tests (pytest)
docs/audit/                 # design + audit reports
```

## Development

```bash
make test     # py_compile + pytest (offline, no panel needed)
make lint     # shellcheck + py_compile
make zip      # build javahost.zip
```

## Status

Early (`v0.1.0`) — core library, installer, services, deploy, and tests are in
place and unit-tested offline. Runtime validation on a clean Ubuntu 24.04 host
is the next milestone (see `docs/audit/Java_Manager_Plugin_Compatibility_Matrix.md`).

## Contributing

Issues and PRs welcome. Please keep the project clean-room: never paste aaPanel
source, UI, or assets. See `docs/audit/Java_Manager_OSS_Port_Strategy.md`.
