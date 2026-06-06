# Java runtime

The Java layer lives in `core/runtime/java.py` and `core/runtime/jvm_opts.py`.
It detects existing JDKs, installs verified Temurin builds when missing, and
exposes a per-runtime `JAVA_HOME` without silently mutating system-wide
`alternatives`.

## Supported versions

Java majors **8, 11, 17, 21** are supported. `validate.java_major` rejects
anything outside `{8, 11, 17, 21}` (fail closed). The install routine
(`install_temurin`) accepts the same four majors.

## Detection order

`detect()` walks `_SEARCH` (newest first) and keeps the first hit per major:

1. `/www/server/javahost/runtimes/jdk-21`, `jdk-17`, `jdk-11`, `jdk-8`
   (JavaHost-managed runtimes).
2. `/usr/local/btjdk/jdk21`, `jdk17`, `jdk11`, `jdk8` (panel-provided JDKs).
3. `/usr/lib/jvm/*` (distro JDKs; the directory is expanded and scanned
   reverse-sorted).
4. Finally `java` on `PATH` (resolved via `realpath`, two dirs up).

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
