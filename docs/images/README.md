# Screenshots — manifest

This directory holds the screenshots referenced by the docs (chiefly
[`../user-guide.md`](../user-guide.md)). The Markdown links each file by the
**exact** name below; keep filenames as listed so the links resolve.

> **Status:** captured against the live **v0.16.2 UI in Fullscreen mode**
> (2026-06-06) and committed here. The `drawer-overview` / `drawer-site-ssl`
> shots and the new `metrics.png` reflect the **v0.16.2 drawer fixes** (real
> CPU%/uptime/threads, runtime-missing signal, neutral Site & SSL for HTTP-only
> apps). The one exception is `hardening-banner.png`, which only appears when
> aaPanel "System Hardening" has locked the service directories — capture it on a
> hardened host and drop it in. To refresh any shot, follow the capture notes
> below (Fullscreen-first).

## How to capture

- Open JavaHost in aaPanel and click the header **Fullscreen** toggle first —
  most shots read better at full viewport (the modal is cramped). `fullscreen.png`
  itself shows the toggled state.
- Light theme is fine; the UI also has a dark theme via `prefers-color-scheme`.
- Use a real install with a couple of apps and at least one reverse-proxy site so
  the health pills, runtime chips, Site & SSL block, and DB env have content.
- Crop to the relevant panel/dialog; redact any real domain/host/secret.

## Shots

| File | Section + state to capture | Caption |
|------|----------------------------|---------|
| `fullscreen.png` | Header **Fullscreen** toggle engaged — plugin filling the whole viewport (not the aaPanel modal). | JavaHost in fullscreen mode, popped out of aaPanel's modal. |
| `install-import.png` | aaPanel **App Store → Third-party → Import plugin**, with `javahost.zip` selected. | Importing the plugin ZIP into aaPanel. |
| `dashboard.png` | **Dashboard** tab: the four stat tiles + Java / Tomcat / Environment cards (`GetStatus`). | The Dashboard — runtimes, Tomcat versions, app counts, service backend. |
| `hardening-banner.png` | **Dashboard** with the red **"System hardening is active"** banner + *Allow services* button (only shows when `service_dirs_locked`). | The hardening banner and its one-click Allow-services action. |
| `runtimes-java.png` | **Runtimes** tab, **Java** card: rows for 8/11/17/21 with installed badges and **Install / Reinstall / Uninstall** actions. | Java runtimes — install, reinstall, uninstall per major. |
| `runtimes-tomcat.png` | **Runtimes** tab, **Tomcat** card: 9 / 10.1 / 11 rows with patch, namespace, min-Java, and Install/Update/Uninstall. | Tomcat lines — install, update, uninstall with enforced Java floors. |
| `java-in-use.png` | The **"Java N is in use"** dialog after attempting to uninstall an in-use JDK — count + scrollable dependents list + **Force**. | Uninstall blocked: the JDK's dependent apps, with a Force override. |
| `applications.png` | **Applications** tab: the rich list — type, runtime chip, status badge, **health pill**, inline Start/Stop/Restart, Open ↗, HTTPS toggle. (Show one row with a **"runtime missing"** badge if available.) | The Applications list with health pills, runtime chips, and inline lifecycle. |
| `create-app.png` | The **Create app** dialog: name / Tomcat version / **Java (JDK pin)** / port / memory. | Creating a Tomcat app, with the optional JDK pin. |
| `drawer-overview.png` | An app row clicked open → the slide-over **drawer**, **Overview** tab (type/runtime/status/health, Live CPU%, uptime, threads, Open link, HTTPS toggle; a **runtime-missing** row if the pinned JDK is gone). | The app detail drawer — Overview with live metrics. |
| `drawer-site-ssl.png` | Same drawer, the **Site & SSL** block (`GetSiteStatus`): domain, cert validity/expiry, HTTP→HTTPS redirect, HTTPS reachability — shown here in the **neutral HTTP-only** state (HTTP only / not enabled / no cert) for an app without SSL. | The drawer's Site & SSL block — neutral for an HTTP-only app. |
| `metrics.png` | The drawer **Metrics** tab: PID, memory, **Live CPU %** (real `/proc` sampling), threads, and uptime. | The drawer's Metrics tab — live CPU%, memory, threads, uptime. |
| `https-toggle.png` | The **per-site HTTPS toggle** (row or drawer Overview) mid-action — `SetSiteSSL`. | Per-site HTTPS toggle (Let's Encrypt, native ACME → certbot fallback). |
| `deploy-war.png` | The **Deploy WAR** dialog: file picker + **Migrate & deploy** option. | Deploying a WAR (with the javax→jakarta migrate option). |
| `deploy-jar.png` | The **Deploy JAR** dialog: Java major + Spring profiles + fat-JAR picker. | Deploying a Spring Boot / executable JAR. |
| `databases-engines.png` | **Databases** tab, top **support matrix** (`GetDbSupport`): engine / version range / port / driver / local detection. | The database engines support matrix. |
| `databases-filter.png` | **Databases** tab, per-app env section with the **search/filter** active and the live count. | Filtering the per-app database env list. |
| `db-env.png` | The **Configure database env** form: engine / version / host / port / db / user / password / **SSL** checkbox (`SetDbEnv`). | Writing a secret-safe per-app database env. |
| `db-current-env.png` | The drawer **Database** tab showing the **current** env (`GetDbEnv`): engine, URL (no password), user, driver, "password set". | The drawer's current DB env panel — secret-safe (no password). |
| `tasks.png` | **Tasks** tab: background jobs with status (`running`/`done`/`failed`), elapsed, and **view-log** (`GetJobs`). | The Tasks list — background install/uninstall/lifecycle jobs. |
| `logs.png` | **Logs** tab: the unified app + task log viewer (line-count selector + Refresh). | The unified Logs viewer (app logs + task logs). |
| `settings-danger.png` | **Settings** tab, **Danger zone**: per-category checkboxes (apps/jdks/tomcats/sites/full), dry-run **Preview**, typed **WIPE** confirm. | Settings → Danger zone — granular wipe with preview and typed confirm. |
| `help-proxy.png` | **Help** tab, **Reverse-proxy hint** card (`GetProxyHint`): the Nginx include snippet. | The Help tab's reverse-proxy include snippet. |
| `dashboard.png` | **Dashboard** with the v0.17+ operational tiles (apps running, aggregate CPU/RSS, SSL expiry, disk) + cards. | The Dashboard with live operational aggregates. |
| `backups-tab.png` | The **Backups** tab: Storage destinations table, Backups list with per-location badges, and Schedules with destination badges. | The dedicated Backups tab (multi-destination). |
| `storage-destinations.png` | The **Storage destinations** card — multiple S3 profiles (provider/bucket/enabled, Test/Edit/Delete). | Managed S3 storage destinations. |
| `backup-destinations-picker.png` | The **Back up now** modal — app + a multi-select of storage destinations. | Choosing destinations for a backup. |
| `schedules.png` | The **Schedules** card — per-app cron + destination badges + retention. | Scheduled backups to selected destinations. |
| `add-destination.png` | The **Add storage destination** modal — provider-aware, region-specific endpoint (auto-built from provider + region) with a guidance hint. | Adding an S3 destination (region-aware endpoint). |
