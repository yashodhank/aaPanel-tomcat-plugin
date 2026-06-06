# Java runtime

The Java layer lives in `core/runtime/java.py` and `core/runtime/jvm_opts.py`.
It detects existing JDKs, installs verified Temurin builds when missing, and
exposes a per-runtime `JAVA_HOME` without silently mutating system-wide
`alternatives`.

## Supported versions

Java majors **8, 11, 17, 21** are supported. `validate.java_major` rejects
anything outside `{8, 11, 17, 21}` (fail closed). The install routine
(`install_temurin`) accepts the same four majors.

## Self-contained runtimes (no panel-JDK reuse)

JavaHost manages **only its own JDKs**. As of v0.16.0 it **no longer detects or
reuses aaPanel's shared `/usr/local/btjdk`** — that removed the confusing "panel
JDK" rows and an un-removable shared runtime, and makes the plugin fully
self-contained. It manages JDKs it installs under
`/www/server/javahost/runtimes/` and recognises distro JDKs under `/usr/lib/jvm`.

> **Migration note:** on an install that predates 0.16.0, repoint any app pinned
> to `btjdk` at a plugin `runtimes/jdk-*` (recreate the app, or pin its JDK via
> the Create-app form / `prefer_java`).

## Detection order

`detect()` walks `_SEARCH` (newest first) and keeps the first hit per major:

1. `/www/server/javahost/runtimes/jdk-21`, `jdk-17`, `jdk-11`, `jdk-8`
   (JavaHost-managed runtimes).
2. `/usr/lib/jvm/*` (distro JDKs; the directory is expanded and scanned
   reverse-sorted).
3. Finally `java` on `PATH` (resolved via `realpath`, two dirs up).

Each candidate is probed by running `<home>/bin/java -version` and parsing the
banner. `parse_major` handles both the legacy `1.8.0_x` scheme (-> `8`) and the
modern scheme where the first component is the major (`11`, `17`, `21`), with a
fallback regex for `openjdk <major>` banners.

`resolve(min_major, prefer=...)` returns a `JAVA_HOME` satisfying
`>= min_major`, preferring an explicitly requested major if present, otherwise
the highest installed major that meets the floor.

## Temurin install + verification

`install_temurin(major)` queries the Adoptium API
`https://api.adoptium.net/v3/assets/latest/<major>/hotspot?...&image_type=jdk`
for the latest build, then downloads, verifies, and extracts it into
`/www/server/javahost/runtimes/jdk-<major>`:

- Adoptium publishes **SHA-256**, so verification uses `_fetch_with_sha256`
  (the artifact's `checksum`, or the `.sha256.txt` via `checksum_link`). If no
  SHA-256 is available it refuses the artifact rather than installing
  unverified.
- Requires ~400 MB free under the runtimes root (`fs.require_free`).
- Extracts with `tar -xzf --strip-components=1`, marks the dir managed, then
  **re-probes**: if the installed JDK does not report the expected major it
  raises (post-install sanity check).

The installer's `ensure_java(major)` ties this to Tomcat: it calls
`java.resolve(line.min_java)` and, if nothing satisfies the floor, installs
Temurin 17 (for floors `<= 17`) or 21 (for floors `> 17`).

## Install / reinstall / uninstall (async, dependency-aware)

The Runtimes tab manages each Java major with **Install / Reinstall / Uninstall**
buttons. The heavy download+extract runs as a **background job** (see
[Tasks & Logs](user-guide.md#9-tasks--logs)) so a slow Adoptium download can't
time out the panel request:

- **Install** → `StartInstallJava` (or the sync `InstallJava`) → installs Temurin
  for that major under `runtimes/jdk-<major>`.
- **Reinstall** → `StartReinstallJava` → reinstalls the same major (job kind
  `reinstall-java`).
- **Uninstall** → `StartUninstallJava` (or the sync `UninstallJava`). It is
  **blocked while the JDK is in use** by a deployed app: the endpoint returns the
  dependent apps (`in_use_by`) so the UI can warn instead of silently breaking
  them. Passing **`force`** overrides the block — and force-uninstall now also
  **stops the dependent apps** so they go cleanly DOWN rather than lingering as
  zombie JVMs that falsely report healthy.

`GetJavaUsage{version}` returns `{version, in_use_by:[apps]}` so the UI can
preview dependents before an uninstall. (Because JavaHost is self-contained, there
is no longer an un-removable panel JDK; every managed JDK can be uninstalled.)

## `runtime_ok` and the "runtime missing" badge

Each app pins a `JAVA_HOME`. If that JDK is later removed (e.g. a force-uninstall),
an already-running app keeps serving on its live JVM but **won't survive a
restart**. `list_apps()` reports **`runtime_ok`** (true when the pinned
`JAVA_HOME` still exists); when it's false the UI shows a red **"runtime missing"**
badge so the misleading "up" status is called out. Recreate the JDK (Install /
Reinstall) or repoint the app to a present runtime to clear it.

## Per-runtime JAVA_HOME

Each Tomcat instance pins its own `JAVA_HOME` in `<base>/bin/setenv.sh` and in
its service unit's `Environment=JAVA_HOME=...`. JAVA_HOME always comes from the
resolved/installed runtime path, never parsed from a shebang — different apps
can run on different JDKs side by side.

## JVM flag validation (`jvm_opts.py`)

`sanitize(opts, java_major)` cleans an option list and returns
`(cleaned, warnings)` so a Java-8-era flag set cannot stop Tomcat from booting
on Java 17/21:

- Drops anything not matching `_SAFE_OPT` (`^[A-Za-z0-9_:+\-=.,/%@${}]+$`) as a
  shell-injection guard.
- On **Java >= 11**, removes the CMS/PermGen-era collector flags in
  `_REMOVED_11_PLUS`: `-XX:+UseConcMarkSweepGC`, `-XX:-UseConcMarkSweepGC`,
  `-XX:+CMSIncrementalMode`, `-XX:+UseParNewGC`, `-Xincgc`. CMS was removed in
  JDK 14 and these are unrecognized/fatal on modern JVMs.
- On **Java >= 9**, removes prefix-matched legacy flags `_REMOVED_PREFIX_8`:
  `-XX:PermSize=`, `-XX:MaxPermSize=`, `-XX:+CMS`, `-XX:CMS`. PermGen was
  removed in Java 8/Metaspace, and `-XX:MaxPermSize` warns/errors on Java 9+.

So on **17 and 21** every CMS and PermGen flag above is stripped (each emits a
"removed flag unsupported on Java N" warning surfaced to the UI), because those
collectors/regions no longer exist and the JVM would refuse to start.

`default_opts(heap_mb)` returns modern-safe defaults: `-server`,
`-Xms<heap/2>m`, `-Xmx<heap>m`, `-XX:+UseG1GC`,
`-Djava.security.egd=file:/dev/urandom`, `-Dfile.encoding=UTF-8`.
