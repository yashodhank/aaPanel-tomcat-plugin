# Running JavaHost with aaPanel "System Hardening" enabled

aaPanel's **System Hardening** (系统加固 / tamper-protection) locks critical system
locations to block persistence attacks. JavaHost now **operates safely under
hardening on its own** — you no longer have to disable anything to create or
manage Java apps.

## What hardening does (observed)

On a hardened host you'll see:

- `/etc/systemd/system` and `/etc/init.d` carry the **immutable bit** — `lsattr -d`
  shows `----i---------e-------`. Even `root` gets `Operation not permitted` (EPERM)
  trying to create a unit/script there.
- `su` to another user (e.g. `www`) is intercepted ("Tips from BT security").

> Note: Tomcat/JDK **install**, **WAR deploy**, **port allocation**, and **config
> rendering** all work fine under hardening — the only friction was *service
> registration* and *run-as-www*, both of which JavaHost now handles transparently.

## Auto-safe service registration (default)

Running as the panel's `root`, JavaHost manages services without weakening your
hardening posture. When a service directory (`/etc/systemd/system` or
`/etc/init.d`) is immutable, the plugin:

1. **Momentarily lifts the immutable bit** on the service directory only long
   enough to create (or remove) its own `javahost-<app>` unit/script — creating a
   file in an immutable directory requires the directory bit to be lifted briefly;
   the plugin touches only its own units and never another plugin's files.
2. **Writes the unit**, then
3. **Immediately re-applies the immutable bit** on the directory, restoring
   hardening before the operation returns.

The window is as short as a single write, and the system is never left
unhardened. Every lift and every restore is **logged** so the action is fully
auditable.

### Run-as-www without `su`

JavaHost no longer uses `su` (which aaPanel security jails). Instead:

- **systemd** services run the app via `User=www` in the unit.
- The **init.d** fallback uses `runuser` to drop to the `www` user.

Both avoid the jailed `su` path entirely.

## The `manage_hardening` toggle

The auto-safe behavior is controlled by `manage_hardening` in
`/www/server/javahost/config.json`. It **defaults to `true`**.

```json
{
  "manage_hardening": true
}
```

- `manage_hardening: true` (default) — the plugin performs the brief
  lift → write → re-lock cycle described above and logs each step.
- `manage_hardening: false` — the plugin will **not touch immutable bits**. If a
  service directory is locked it returns the previous clear error and leaves the
  decision to you:

  > cannot install a service: both /etc/systemd/system and /etc/init.d are not
  > writable (likely immutable via chattr +i — e.g. aaPanel 'System Hardening').
  > Disable system hardening / lift the lock, then retry.

## Status flag and UI banner

`GetStatus` still reports a `service_dirs_locked` flag, but it is now `true`
**only when the plugin genuinely cannot manage services** — that is, when the
directories are immutable **and** `manage_hardening` is disabled, **or** when
`chattr` is unavailable on the host. With the default settings on a hardened
host, the flag is `false` and the dashboard shows **no** lock banner, because
service management works. The UI banner reflects this flag directly.

## Security rationale

- **Never leaves the system unhardened.** The immutable bit is re-applied
  immediately, in the same operation, before returning.
- **Touches only its own paths.** Only the specific `javahost-<app>` unit/script
  is unlocked — not the whole directory, and never another plugin's files.
- **Fully auditable.** Every lift and restore is logged.
- **Opt-out available.** Set `manage_hardening: false` to keep the plugin from
  touching immutable bits at all and fall back to manual handling.

## Manual options (when `manage_hardening: false`)

If you turn auto-handling off, pick one of these to let JavaHost manage services:

1. **Disable System Hardening in the aaPanel UI** while you create/manage apps:
   aaPanel → **Security** / **System Hardening** (系统加固) → turn off, do your
   JavaHost operations, then turn it back on. Fully reversible.

2. **Temporarily lift the lock on the service dir** (root), create the app, then
   restore it:
   ```bash
   chattr -i /etc/systemd/system        # lift
   # ... create your app in JavaHost ...
   chattr +i /etc/systemd/system        # restore hardening
   ```
   Re-enabling System Hardening afterwards is recommended.

3. **Run on a host without tamper-protection** if your policy allows it.

## Three hardening layers — what JavaHost automates vs. what you authorize

Aggressive aaPanel hardening has **three** independent controls. JavaHost
automates the first two and **detects** the third:

1. **Immutable service directories** (`chattr +i` on `/etc/systemd/system`,
   `/etc/init.d`) — **auto-handled**: lift → write → re-lock (above). No action.
2. **syssafe "Abnormal process" killer** (`process_white` / `process_white_rule`
   allowlist) — **auto-registered**: call **`AllowServices`** (a one-click action /
   endpoint) and JavaHost *appends* its markers (`/www/server/javahost`,
   `catalina.sh`, `jsvc`) to syssafe's own allowlist (append-only, backed up,
   reversible). This registers — it never bypasses.
3. **Global LD_PRELOAD execve filter** (aaPanel **bt_security** / **usranalyse**,
   via `/etc/ld.so.preload`) — this is what emits `status=203/EXEC` +
   `Tips from BT security !!!` and keeps a new service in `activating (auto-restart)`.
   It is a host-level anti-persistence agent with its **own** enable/disable
   (`/usr/local/usranalyse/sbin/usranalyse-{disable,enable}`) and config — **not**
   governed by syssafe's allowlist.

JavaHost **detects layer 3 and stops with a clear error** (also surfaced via
`GetStatus.exec_filter_active`). It deliberately does **not** disable or patch a
global security preload:

> service installed but aaPanel process/daemon protection blocked it from
> executing (status 203/EXEC, 'Tips from BT security'). … JavaHost will NOT bypass
> anti-persistence controls.

**Why not bypass it?** Defeating an anti-persistence / anti-webshell exec filter
is exactly what malware does. A legitimate management plugin must not, so JavaHost
asks *you* to authorize it instead.

**To allow JavaHost services to run under layer 3 (the execve filter)**, do one of:

- Run **`AllowServices`** first (registers JavaHost in syssafe's allowlist — layer
  2), then in aaPanel **Security → bt_security** authorize JavaHost (or the
  `/www/server/javahost` path) and **Repair** the app.
- Temporarily disable the exec filter while creating apps, then re-enable it:
  `/usr/local/usranalyse/sbin/usranalyse-disable` … `usranalyse-enable`.
- Run JavaHost on a host without the LD_PRELOAD exec filter.

Note: Tomcat/JDK **install**, **WAR/JAR deploy**, **port allocation**, **config
rendering**, and even **direct foreground execution** all work under every layer —
only *registering and auto-starting a managed service* requires layer-3 approval.

## Related installer behaviour (works regardless of hardening)

- Shared `CATALINA_HOME` is made group/other `r-X` so the `www` run-user can
  execute `catalina.sh` (Apache tarballs ship `bin/*.sh` as `0750`).
- Each per-app `CATALINA_BASE` gets the default `conf/web.xml` (DefaultServlet +
  welcome files) and is `chown`ed to the run user — otherwise `/` returns 404.
