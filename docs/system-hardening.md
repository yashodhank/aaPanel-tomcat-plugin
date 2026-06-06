# Running JavaHost with aaPanel "System Hardening" enabled

aaPanel's **System Hardening** (系统加固 / tamper-protection) locks critical system
locations to block persistence attacks. When it is on, JavaHost **cannot register
a service**, because the directories it needs are made immutable and `su` is jailed.

## What hardening does (observed)

On a hardened host you'll see:

- `/etc/systemd/system` and `/etc/init.d` carry the **immutable bit** — `lsattr -d`
  shows `----i---------e-------`. Even `root` gets `Operation not permitted` (EPERM)
  trying to create a unit/script there.
- `su` to another user (e.g. `www`) is intercepted ("Tips from BT security").

JavaHost detects this and **fails with a clear message** instead of a cryptic
EPERM:

> cannot install a service: both /etc/systemd/system and /etc/init.d are not
> writable (likely immutable via chattr +i — e.g. aaPanel 'System Hardening').
> Disable system hardening / lift the lock, then retry.

The dashboard also shows a **"System Hardening locked"** banner (from `GetStatus`'s
`service_dirs_locked` flag) before you try to create an app.

> Note: Tomcat/JDK **install**, **WAR deploy**, **port allocation**, and **config
> rendering** all work fine under hardening — only *service registration* and
> *run-as-www* are blocked.

## How to allow JavaHost to manage services

Pick one (in order of preference):

1. **Disable System Hardening in the aaPanel UI** while you create/manage apps:
   aaPanel → **Security** / **System Hardening** (系统加固) → turn off, do your
   JavaHost operations, then turn it back on. This is the cleanest, fully reversible
   path.

2. **Temporarily lift the lock on the service dir** (root), create the app, then
   restore it:
   ```bash
   chattr -i /etc/systemd/system        # lift
   # ... create your app in JavaHost ...
   chattr +i /etc/systemd/system        # restore hardening
   ```
   Re-enabling System Hardening afterwards is recommended.

3. **Run on a host without tamper-protection** if your policy allows it.

## Why JavaHost doesn't auto-disable hardening

It deliberately will not silently weaken your security posture. It detects the
lock, tells you exactly what's blocked and why, and leaves the decision to you.

## Related installer behaviour (works regardless of hardening)

- Shared `CATALINA_HOME` is made group/other `r-X` so the `www` run-user can
  execute `catalina.sh` (Apache tarballs ship `bin/*.sh` as `0750`).
- Each per-app `CATALINA_BASE` gets the default `conf/web.xml` (DefaultServlet +
  welcome files) and is `chown`ed to the run user — otherwise `/` returns 404.
