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

## Two hardening layers — what JavaHost handles vs. what you must allow

Aggressive aaPanel hardening has **two** separate controls:

1. **Immutable service directories** (`chattr +i`) — JavaHost handles this
   automatically (lift → write → re-lock, above). No action needed.
2. **Daemon / process protection** (`daemon_service_lock` / "BT security") —
   actively **blocks newly-created services from *executing*** their binary. You'll
   see `status=203/EXEC` and `Tips from BT security !!!` in the journal, and the
   service stays `activating (auto-restart)`.

JavaHost **detects layer 2 and stops with a clear error** — it deliberately does
**not** try to bypass it:

> service installed but aaPanel process/daemon protection blocked it from
> executing (status 203/EXEC, 'Tips from BT security'). … JavaHost will NOT bypass
> anti-persistence controls.

**Why not bypass it?** Defeating an anti-persistence / anti-webshell exec filter
is exactly what malware does. A legitimate management plugin must not, so JavaHost
asks *you* to authorize it instead.

**To allow JavaHost services to run under layer 2**, do one of:

- In aaPanel **Security → daemon/process protection**, **whitelist** the
  `javahost-*` services (or the `/www/server/javahost` path), then **Repair** the app.
- Temporarily disable that specific protection while creating apps, then re-enable it.
- Run JavaHost on a host without daemon-exec protection.

Note: Tomcat/JDK **install**, **WAR/JAR deploy**, **port allocation**, **config
rendering**, and even **direct foreground execution** all work under both layers —
only *registering and auto-starting a managed service* requires layer-2 approval.

## Related installer behaviour (works regardless of hardening)

- Shared `CATALINA_HOME` is made group/other `r-X` so the `www` run-user can
  execute `catalina.sh` (Apache tarballs ship `bin/*.sh` as `0750`).
- Each per-app `CATALINA_BASE` gets the default `conf/web.xml` (DefaultServlet +
  welcome files) and is `chown`ed to the run user — otherwise `/` returns 404.
</content>
</invoke>
