# Installing JavaHost

JavaHost is an independent, open-source (Apache-2.0) plugin for aaPanel /
BaoTa-style panels. There are two supported install paths: importing the
release ZIP through the panel UI, or deploying from source to your own panel.

## Requirements

- A running aaPanel / BaoTa-style panel with the Python 3 plugin runtime
  (the panel ships its own interpreter at `/www/server/panel/pyenv/bin/python`).
- Linux host with **systemd** (an init.d fallback is used automatically on hosts
  without systemd — see [troubleshooting](docs/troubleshooting.md)).
- Host tools used at runtime:
  - `curl` — artifact downloads (a `urllib` fallback is used if curl is absent).
  - `gpg` — OpenPGP signature verification of Tomcat/JDK downloads. Without it,
    SHA-512 is still enforced; signatures are skipped.
  - `tar` — extracting the verified Tomcat tarball.
- A `www` user/group (the default service account Tomcat instances run as).
- Outbound HTTPS to Apache and Adoptium, unless you use the offline path below.

## Option A — ZIP import (recommended)

1. Download `javahost-v<version>.zip` from the project
   [Releases](https://github.com/yashodhank/aaPanel-tomcat-plugin/releases).
   Each release also publishes a `.sha256` you can verify before importing:
   ```bash
   sha256sum -c javahost-v<version>.zip.sha256
   ```
2. In the panel: **App Store → Third-party → Import Plugin**, and select the ZIP.
3. The panel runs `install.sh install`, which creates the data root and
   registers the plugin. JavaHost then appears in your installed apps.

The ZIP contains the `javahost/` plugin directory (manifest, entrypoint, UI,
install hooks, and the `core/` library). It does **not** bundle Tomcat or any
JDK — those are downloaded and integrity-verified on first use.

## Option B — Deploy from source (your own panel)

Use this for development against a panel you control. It rsyncs the plugin and
restarts the panel:

```bash
make deploy VPS_HOST=root@your-server
```

`VPS_HOST` defaults to the value in the `Makefile`; always override it. The
target sync path is `/www/server/panel/plugin/javahost`. `make deploy` excludes
`__pycache__`/`*.pyc`, then runs `make restart` (clears pycache and runs
`/etc/init.d/bt restart` on the remote).

To build the ZIP locally instead of using a release:

```bash
make zip        # -> javahost.zip (plugin/javahost, minus pycache)
```

## Data directory layout

`install.sh` provisions the data root at `/www/server/javahost` (matches the
`checks` field in `info.json`):

```
/www/server/javahost/
├── runtimes/      # installed JDKs (Temurin), per-runtime JAVA_HOME
├── tomcat/        # shared CATALINA_HOME per major: tomcat/9, tomcat/10, tomcat/11
├── instances/     # per-app CATALINA_BASE (conf, webapps, logs, work, temp, bin)
├── vhost/nginx/   # plugin-owned reverse-proxy vhosts (*.conf)
└── .keys/         # OpenPGP keyrings for download verification (mode 0700)
```

Each managed Tomcat home and each app instance carries a `.javahost-managed`
marker. Removal helpers refuse to delete anything that lacks the marker or lies
outside these roots.

### Uninstall

The panel runs `install.sh uninstall`, which removes only the plugin code and
icon by default. Managed runtimes and apps under `/www/server/javahost` are kept
unless you opt in:

```bash
PURGE=1 install.sh uninstall   # also removes /www/server/javahost
```

## Offline / air-gapped Tomcat install

Downloads are fail-closed: an artifact with no SHA-512 is refused. For hosts
without outbound access, supply a local tarball plus its expected hash. The CLI
wrapper delegates to the verified Python installer (it has no download logic of
its own, so verification cannot be bypassed):

```bash
plugin/javahost/tomcat_install.sh install 11 \
    --local /path/to/apache-tomcat-11.x.y.tar.gz \
    --sha512 <hex-from-apache-.sha512>
```

Other flags: `--patch X.Y.Z` (pin a specific patch) and `--prefer-java N`
(choose which detected JDK to use, subject to the version floor). The Java floor
is enforced before anything is written: Tomcat 10.1 needs Java 11+, Tomcat 11
needs Java 17+. If no suitable JDK is present, a Temurin JDK is installed
(verified) automatically.

See also: [Troubleshooting](docs/troubleshooting.md) and
[Connecting Java apps to databases](docs/databases-java-apps.md).
