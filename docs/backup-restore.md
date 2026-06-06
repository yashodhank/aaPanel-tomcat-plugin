# Backup, restore & remote storage

JavaHost can archive a deployed app, restore it (in place or as a new app), push
backups to S3-compatible object storage, run them on a schedule with retention,
and restore from an uploaded file. Everything runs through the same panel-agnostic
`core/backup/` library and the async job system, and every archive is unpacked
through one hardened tar extractor.

## What a backup captures

A backup is a gzip tarball `backup-<app>-<UTCstamp>.tar.gz` containing:

- `manifest.json` — app name, type (war/jar), Tomcat/Java major, memory, port,
  domain, `ssl_enabled`, db engine, created-at, plugin version.
- `base/conf/` — `server.xml`, `context.xml`, `web.xml`.
- `base/webapps/` — the deployed app (e.g. `ROOT`).
- `base/bin/` — `setenv.sh`, **`app.env`** (DB credentials), `site.domain`,
  `site.ssl` (the marker; **not** a key).
- `base/app.jar` — for Spring Boot / executable-JAR apps.
- `nginx/<app>.conf` — the plugin-owned reverse-proxy vhost.

**Deliberately excluded:** `logs/`, `work/`, `temp/` (regenerated at runtime); the
systemd/init.d unit (re-rendered on restore — a unit file is never unpacked from an
archive); and **all of `/etc/letsencrypt`**. TLS **private keys are never bundled**
— on restore the certificate is **re-issued** (see below).

> Backups contain the app's DB credentials (`bin/app.env`), so archives are written
> `0600` under the managed backups dir (`/www/server/javahost/backups`). Secure any
> remote bucket they are uploaded to.

## Backing up

- **UI:** Applications → **Backups** card → **Back up now** → pick an app.
- **Endpoint:** `StartBackup{app, remote?}` → runs as an async job (`ListBackups`,
  `DeleteBackup` round it out). With `remote=1` the archive is also uploaded to the
  configured object storage.

## Restoring

Two modes:

| Mode | When | What happens |
|------|------|--------------|
| **Overwrite** | no new name given | The original app is stopped and removed, then restored in place with its original port and domain. |
| **Restore as new** | a new name is given | A separate app is created on a **reallocated port** (`server.xml`/`app.env` rewritten); the domain is **remapped** only if you supply one, otherwise the site is dropped so two apps never collide. |

- **UI:** Backups card → **Restore** on a row → optionally enter a new name + domain.
- **Endpoint:** `StartRestore{archive, as_name?, domain?}` (async). If the archive
  is only in remote storage it is downloaded first.

**SSL on restore is best-effort.** If the source had SSL, restore re-issues the
certificate via `SetSiteSSL` (aaPanel-native → certbot fallback). If issuance fails
(rate limit, DNS not yet pointed), the restore still succeeds on HTTP and reports an
`ssl_warning` — no keys are ever carried in the archive.

## Remote object storage (S3-compatible)

Settings → **Remote storage**. Works with **Wasabi, MinIO, Backblaze B2, Cloudflare
R2 and AWS S3** via a dependency-free client (stdlib AWS SigV4) that accepts a custom
`endpoint`. Fields: provider, endpoint URL, region, bucket, access key, secret key,
optional path prefix, and **path-style addressing** (on for Wasabi/MinIO).

- Credentials are stored server-side in `/www/server/javahost/remote.json` (`0600`);
  the **secret key is never returned to the UI** (only a "set" flag).
- **Test connection** does a `HEAD` on the bucket.
- Endpoints: `GetRemoteStorage` (redacted), `SetRemoteStorage`, `TestRemoteStorage`,
  `RemoveRemoteStorage`.

> Single-PUT uploads are capped at 5 GB (S3 limit); multipart is not implemented —
> app backups are typically far smaller.

## Scheduled backups + retention

Settings → **Scheduled backups** → **Add schedule**: pick an app, a frequency
(daily / weekly / hourly / every-6-hours / custom cron), a retention count, and
whether to also upload remotely.

- Schedules are stored in `/www/server/javahost/schedules.json` and the managed file
  `/etc/cron.d/javahost-backups` is regenerated from it (hardening-aware: the
  immutable bit on `/etc/cron.d` is briefly lifted and re-locked, like service units).
- Each run invokes `core/backup/run.py` (backup + prune). **Retention** keeps the
  newest *N* backups for that app, locally **and** remotely.
- Cron expressions are validated to 5 fields of `[0-9*/,-]` only (no shell surface).
- Endpoints: `GetBackupSchedules`, `SetBackupSchedule{app, cron, remote?, keep?}`,
  `RemoveBackupSchedule{app}`.

## Restore from upload

Backups card → **Restore from file** → choose a `.tar.gz`. The panel stages the
upload and `StartRestoreUpload{tmp, as_name?, domain?}` restores it. This is the
untrusted-input path, so the archive is unpacked **only** through
`safe_extract_tar`, which realpath-contains every member and **rejects** symlink,
hardlink, device/fifo, absolute-path and `..` entries.

## Security summary

- One hardened extractor for every restore (local, remote, upload).
- Archives never contain `/etc/letsencrypt` (keys); SSL is re-issued.
- Backups carry DB credentials → `0600`, managed dir, documented.
- Remote secret key stored `0600`, never echoed to the UI.
- All names validated (`validate.identifier`/strict archive-name regex); cron
  expressions validated; remote restore downloads to the managed backups dir.
