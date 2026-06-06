# Java Manager — OSS Port Strategy

**Date:** 2026-06-06
**Decision owner:** @yashodhank
**Companion:** see `Java_Manager_Existing_Plugin_Audit_Report.md` (license & findings)

---

## 1. Fork vs clean-room decision — **CLEAN-ROOM REWRITE**

| Option | Verdict | Why |
|--------|---------|-----|
| Direct fork allowed | ❌ No | aaPanel license §2.2 forbids public release of modified source; §4.3 forbids changes when redistributing. |
| Port with attribution allowed | ❌ No | Attribution does not cure §2.2/§4.3; "All rights reserved" headers, no permissive grant. |
| **Clean-room rewrite recommended** | ✅ **Yes** | License §3.1/§3.2 explicitly permits independent **API-based applications** ("released for free or for a fee") that contain **no aaPanel code**. |
| License unclear, do not copy | ⚠️ Partial | License is *clear* that public redistribution of derivatives is prohibited — so this collapses into "clean-room." |

**Rule for the project:** the copied files (`tomcat2_main.py`, `index.html`, `info.json`, `icon.png`) are retained **locally, privately, as a behavioral spec only**. Every line of the new plugin is written from that spec, not pasted. No aaPanel source, strings, layout, or icons ship in the public repo.

**Immediate containment (Phase 0, do before anything else):**
1. `gh repo edit yashodhank/aaPanel-tomcat-plugin --visibility private` (until clean replacements exist), **or** remove the four copied files + purge from history (`git filter-repo`).
2. Replace `icon.png` (aaPanel asset) with an original icon.
3. Add `LICENSE` (Apache-2.0 recommended — patent grant suits a deploy tool) once original code exists.
4. `.gitignore` build artifacts (`graphify-out/`, `.graphify_*`, `*.zip`).

---

## 2. Plugin name & identity recommendation

| Field | Old (must not reuse) | New |
|-------|----------------------|-----|
| `name` | `tomcat2` | `javahost` (proposed; unique, not aaPanel's) |
| `id` | `10000` (aaPanel's) | omit / use a high unused value; do **not** reuse 10000 |
| `title` | "Java Project Manager (Patched)" | "JavaHost — Tomcat & Java Runtime Manager" |
| `author` | "community-fork" | "yashodhank" + project URL |
| Repo | `aaPanel-tomcat-plugin` | keep, or rename `javahost-aapanel` |
| Icon | `ico-tomcat2.png` | original SVG/PNG |

Using a distinct `name`/`id` is also what makes **coexistence** with the official `tomcat2` plugin possible (no path/manifest collision).

---

## 3. Current-state map (reuse decisions)

| Area | Existing Behavior | Reusable? | Needs Rewrite? | Notes |
|------|-------------------|-----------|----------------|-------|
| Plugin manifest (`info.json`) | aaPanel schema, name=tomcat2 | Schema yes / content no | Rewrite | New name/id/author/icon; same schema is a public API contract (§3 allowed). |
| Install hook (`install.sh`) | copies icon, mkdir plugin dir | Pattern yes / code no | Rewrite | Trivial; rewrite cleanly with new paths. |
| Tomcat installer (`tomcat_install.sh`) | DL+extract+init.d+symlink | **Concept yes** | **Rewrite + harden** | Add checksum/GPG, systemd, hardening, JDK 21, fix B1/B2. Your own work but co-derived with aaPanel branch — rewrite to be unambiguously original. |
| Backend controller (`tomcat2_main.py`) | per-site CATALINA_BASE + ElementTree | **No** | **Full clean-room** | Proprietary copy. Re-implement from spec with subprocess arg-lists, templated config. |
| Legacy server.xml class (`class/tomcat.py`) | ElementTree edits (2015) | No | Replace | Use Jinja templates over a known-good `server.xml` per major version. |
| UI (`index.html`) | aaPanel jQuery layout | **No (layout is IP, §5)** | **Full clean-room** | Rebuild UI from scratch; new markup/strings. |
| Java detection (`javaModel`/`utils`) | `java -version` parse, btjdk paths | Behavior yes / code no | Rewrite | Robust parser; per-Tomcat JAVA_HOME; 8/11/17/21. |
| Service mgmt | init.d + chkconfig | No | Rewrite → **systemd** | systemd units with init.d fallback for non-systemd. |
| Reverse proxy (`server_proxy.py`) | Nginx vhost gen | Behavior yes / code no | Rewrite | Generate plugin-owned vhost; never edit foreign configs. |
| PostgreSQL (`pgsql_adapter.py`) | connection helpers | Behavior reference | New (guidance-only) | JDBC string template + env-file; no DB management. |
| v2 adapters (`tomcat_adapter.py`) | dict-contract wrapper | Pattern yes / code no | Rewrite | Good design pattern to mirror in your own module. |

---

## 4. Target architecture (clean-room)

```
javahost/                      # plugin root (deployed to /www/server/panel/plugin/javahost)
├── info.json                  # manifest (new id/name/icon)
├── install.sh / uninstall.sh / update.sh
├── icon.png                   # original
├── index.html + static/       # original UI (no aaPanel markup)
├── javahost_main.py           # thin aaPanel entrypoint → delegates to core/
└── core/
    ├── runtime/
    │   ├── java.py            # JDK detect/install (8/11/17/21), per-runtime JAVA_HOME
    │   └── jvm_opts.py        # flag validation, strip unsupported flags for 17/21
    ├── tomcat/
    │   ├── registry.py        # version model (9 legacy / 10.1 / 11) + min-JDK + URLs
    │   ├── installer.py       # download → verify (sha512+gpg) → extract → harden
    │   ├── hardening.py       # remove examples/docs, manager lockdown, shutdown port
    │   ├── service.py         # systemd unit gen (+ init.d fallback)
    │   └── templates/         # server.xml / context.xml / setenv.sh per major version
    ├── deploy/
    │   ├── war.py             # WAR/exploded deploy, zip-slip-safe extraction
    │   └── proxy.py           # plugin-owned Nginx vhost
    ├── db/pg.py               # PostgreSQL 17 JDBC guidance + env-file
    ├── compat/aapanel.py      # the ONLY module touching panel internals (public.ExecShell, paths)
    └── util/{shell.py,validate.py,fs.py,download.py}
```

**Key principles:**
- **One compat boundary** (`core/compat/aapanel.py`): all panel-coupling isolated so the core stays portable to BaoTa/forks.
- **No shell string interpolation:** `util/shell.py` runs `subprocess.run([...], ...)` arg-lists only.
- **Legacy vs modern separation:** Tomcat 9 support lives behind a `legacy` flag and never blocks 10/11 paths.
- **Idempotent, marker-based lifecycle:** every managed resource carries a `.javahost-managed` marker; uninstall only removes marked resources.

---

## 5. Migration strategy (spec-extract, then rebuild)

1. **Freeze the spec:** keep `docs/spec/` (private) with behavior notes extracted from the copied files — method list, inputs/outputs, side effects — *no code*.
2. **Skeleton first** (Phase 2): manifest + entrypoint + compat boundary that loads in a panel, does nothing destructive.
3. **Vertical slices:** Java runtime → Tomcat 11 (primary) → Tomcat 10 → legacy 9 → deploy/proxy → PG guidance → hardening → UI.
4. **Parity tests** against the spec (template-render + command-construction unit tests; no production).
5. **Cut over:** publish only after the copied files are gone from history and CI is green.

---

## 6. Attribution / licensing notes
- You **may** state interoperability: "A Tomcat/Java runtime manager plugin for aaPanel/BaoTa-style panels" (factual, allowed).
- You **may not** copy aaPanel code, UI layout, icons, or strings (§5).
- Apache Tomcat (Apache-2.0), OpenJDK/Temurin (GPLv2+CE), PostgreSQL JDBC (BSD-2) are all redistributable/linkable — your plugin downloads them at runtime rather than bundling, which is cleanest.
- Add `NOTICE` documenting third-party runtimes the plugin fetches.
