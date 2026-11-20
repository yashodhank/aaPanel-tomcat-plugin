# Running JavaHost with aaPanel "System Hardening" enabled

aaPanel's **System Hardening** (系统加固) locks system locations to block
persistence attacks. JavaHost works with it the way the panel's own features do:
it never unlocks a directory itself and never edits the hardening plugin's
settings. When a locked directory blocks a write, it asks the panel to pause
hardening for that write, and the panel turns it back on.

## What hardening does

On a hardened host:

- `/etc/systemd/system`, `/etc/init.d` and `/etc/cron.d` can be read-only even
  for `root` (`Operation not permitted`).
- `su` to another user (for example `www`) is intercepted ("Tips from BT security").

Tomcat and JDK installs, WAR and JAR deploys, port allocation and config
rendering are not affected. Only three things write to system locations: service
units, the backup schedule and the log-rotation schedule.

## What JavaHost does when a system directory is locked

1. It tries the write. If that works, nothing else happens and hardening is not
   touched.
2. If the write is refused and `manage_hardening` is on (the default), JavaHost
   calls the panel's own pause helper, the same one the panel uses for its own
   services. The panel stops System Hardening, JavaHost writes its one file, and
   the panel starts hardening again, whether or not the write worked. The pause
   is recorded in the panel's operation log.
3. If System Hardening is not installed, the panel has no pause helper,
   `manage_hardening` is off, or the directory is still read-only after the
   pause, the action fails with a message that tells you to turn System Hardening
   off, retry, and turn it back on.

Things to know about the pause:

- While the panel has hardening paused, the whole feature is off for those few
  seconds, not only one directory. If that is not acceptable on your server, set
  `manage_hardening` to `false` and make service changes in a window where you
  turn hardening off yourself.
- Only one JavaHost task pauses hardening at a time.
- If a task is killed while hardening is paused, JavaHost turns it back on the
  next time the dashboard loads, the next time it writes a system file, or at the
  next daily log rotation, whichever comes first. Until then hardening stays
  off. To turn it on at once, use the System Hardening page in the panel.

JavaHost does **not**:

- change file attributes on system directories,
- add itself to System Hardening's process allowlist,
- disable or patch the host exec filter.

### Run-as-www without `su`

systemd services run the app with `User=www`. The init.d fallback uses `runuser`.
Neither uses the intercepted `su` path.

## The `manage_hardening` setting

In `/www/server/javahost/config.json`, default `true`:

```json
{
  "manage_hardening": true
}
```

- `true`: JavaHost may ask the panel to pause System Hardening for a system write.
- `false`: JavaHost never asks. A blocked write fails with the message above.

## Status flag and banner

`GetStatus` reports `service_dirs_locked`. It is `true` only when JavaHost cannot
manage services at all: the service directories are read-only and either
`manage_hardening` is off, the panel has no pause helper, or System Hardening is
not installed (so something else locked them). The dashboard banner shows
in that case and tells you what to do.

## If a service still will not start

Two other controls can stop a new service. JavaHost reports them and leaves the
decision to you.

1. **Abnormal-process protection** in System Hardening can kill processes it does
   not know. Add `/www/server/javahost` to its process allowlist in the System
   Hardening page of the panel.
2. **The host exec filter** (aaPanel **bt_security** / **usranalyse**, loaded
   through `/etc/ld.so.preload`) blocks new services with `status=203/EXEC` and
   `Tips from BT security !!!`. JavaHost detects it (`GetStatus.exec_filter_active`)
   and stops with a clear error. Authorize JavaHost in **Security → bt_security**,
   or turn the filter off while you create the app
   (`/usr/local/usranalyse/sbin/usranalyse-disable`, then `usranalyse-enable`),
   then **Repair** the app.

## Upgrading from 0.29 or earlier

Older versions unlocked the service directories themselves and had an
**Allow services** button that wrote JavaHost entries into System Hardening's
process allowlist. Both are gone.

Entries that the button already added are left in place, because removing them
would mean editing the hardening settings again. To remove them, open System
Hardening in the panel and delete `/www/server/javahost`, `javahost`,
`catalina.sh`, `jsvc` and `jsvc.exec` from the process allowlist, or restore the
copy saved next to the settings file as `<config>.javahost.bak`.
