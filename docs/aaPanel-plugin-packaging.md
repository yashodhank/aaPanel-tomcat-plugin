# aaPanel plugin packaging (JavaHost)

How JavaHost is structured as an aaPanel / BaoTa-style third-party plugin, how
the panel loads and dispatches to it, and how a release ZIP is built.

## Plugin layout

Everything the panel needs lives under `plugin/javahost/`:

```
plugin/javahost/
├── info.json            # manifest
├── javahost_main.py     # panel entrypoint (thin glue)
├── index.html           # plugin UI
├── icon.svg             # icon
├── install.sh           # install/uninstall hook (panel calls this)
├── tomcat_install.sh    # CLI wrapper -> verified Python installer
└── core/                # portable library (panel coupling isolated in core/compat)
    ├── util/  runtime/  tomcat/  deploy/  db/  compat/
```

On deploy this directory maps to `/www/server/panel/plugin/javahost`.

## info.json fields and the name/id contract

`info.json` is the manifest the panel reads:

| Field | Purpose |
|-------|---------|
| `title` | Display name in the panel UI. |
| `name` | **Plugin id.** Must be `javahost` (see contract below). |
| `tip`, `panel_type`, `type` | Panel categorization (`type: environment`). |
| `ps` | Short description shown in the App Store. |
| `versions` | Current version (e.g. `0.1.0`); release automation syncs this. |
| `shell` | Install hook script name (`install.sh`). |
| `checks` | Path the panel checks to decide "installed": `/www/server/javahost`. |
| `author`, `home`, `date` | Attribution + project URL + release date. |
| `default`, `display` | Listing/visibility flags. |

**The name/id contract:** `name` must equal `javahost` and must match the plugin
directory name (`plugin/javahost`) and the install path
(`/www/server/panel/plugin/javahost`). The panel uses this id to locate the
module, the install hook, and the `checks` path. CI enforces
`info.json["name"] == "javahost"` and that `title`, `name`, `versions`,
`author`, and `type` are all present.

## The install hook

`info.json["shell"]` points at `install.sh`, which the panel invokes as
`install.sh install` (and `install.sh uninstall`). It:

- creates the data root `/www/server/javahost/{runtimes,tomcat,instances,vhost/nginx,.keys}`
  (`.keys` is `chmod 700`),
- best-effort registers an icon,
- on uninstall, removes only plugin code/icon and keeps managed runtimes/apps
  unless `PURGE=1` is set.

## How the panel dispatches requests

The panel imports `javahost_main.py`, instantiates the `javahost_main` class, and
calls `instance.<Method>(get)`, where `get` is an attribute namespace of request
parameters. Conventionally the panel routes by a method selector such as
`s=<Method>`, which maps to the same-named method on the class.

Each public method (`GetStatus`, `InstallJava`, `InstallTomcat`,
`UninstallTomcat`, `UpdateTomcat`, `CreateApp`, `AppAction`, `DeleteApp`,
`RepairApp`, `GetAppDetail`, `GetLogs`, `DeployWar`, `SetDbEnv`, `GetDbSupport`,
`GetProxyHint`) follows the same pattern: read params via `panel.attr(get, ...)`,
**validate every input** (`core/util/validate.py`), call into `core/`, and return
`panel.ok(...)` / `panel.err(...)`.

The entrypoint is deliberately thin — all real logic is in `core/`, keeping the
panel-facing surface small and auditable. The **only** module that touches panel
internals is `core/compat/aapanel.py`, which uses the panel's public, documented
helpers (`public.returnMsg`, `public.GetMsg`, `public.WriteLog`). It degrades
gracefully off-panel (when `import public` fails) so `core/` is unit-testable
without a panel.

## Packaging a ZIP

```bash
make zip        # -> javahost.zip
```

This zips `plugin/javahost` (excluding `__pycache__` and `*.pyc`). The ZIP root
is the `javahost/` directory, which is exactly what the panel's
**App Store → Third-party → Import Plugin** flow expects.

## CI / release flow

- **CI** (`.github/workflows/ci.yml`) on every PR and push to `main`:
  validates required files exist, validates `info.json` (incl. the `javahost`
  name check), confirms no proprietary-derived file (`tomcat2_main.py`) is
  tracked, runs shellcheck, then `py_compile` + `pytest`. On push to `main` it
  builds the ZIP and uploads it as a 90-day artifact.
- **Release** (`.github/workflows/release.yml`) on a `v*` tag (or manual
  `workflow_dispatch` with a version): re-runs tests, syncs `info.json`
  `versions`/`date`, builds `javahost-v<version>.zip` plus a `.sha256`, and
  publishes a GitHub Release with auto-generated notes.
- **Local helper:** `make release [major|minor|patch|X.Y.Z]` (→
  `scripts/release.sh`) computes the next version, checks the tree is clean,
  prints the changelog since the last tag, builds a versioned ZIP, and prints the
  commit/tag/push steps that trigger the release workflow.
