---
name: javahost-teardown
description: >
  Clean the JavaHost test box (the dev VPS) of test state — a FULL teardown
  (uninstall the plugin + wipe everything) or a PARTIAL one mid-development
  (e.g. drop the deployed apps but keep runtimes/DBs for a fast redeploy). Use
  when asked to "clean up the VPS / box", "tear down", "reset the test
  environment", or "uninstall the plugin". Inventory-first, marker-gated, and
  scoped — it never touches the panel, its cert, other plugins, or unrelated
  containers. The deploy counterpart is the javahost-test-deploy skill.
allowed-tools: Bash(ssh root@* *) Bash(docker *)
---

# JavaHost — teardown / cleanup runbook

Cleaning the dev box (`root@<box>` from gitignored `_private_spec/OPS-ACCESS.md`).
Two shapes: **full** (uninstall + wipe everything) or **partial / `dev-reset`**
(clear what you're iterating on, keep the rest). Reuse the plugin's OWN tested
teardown (`core/maintenance.wipe`, `install.sh uninstall`) over hand-rolled `rm`.

## 0. Rules (read first — these are hard constraints)
1. **Inventory before you destroy; verify after.** Both are read-only ssh.
2. **Preserve the panel.** NEVER `certbot delete 5d.bisotech.in` (the panel's own
   cert), never touch the aaPanel install, other plugins, other sites, or any
   non-`javahost` container.
3. **Scoped Docker only.** NEVER `docker volume prune` / `docker system prune`
   unscoped. Inventory each `javahost-demo-*` container's mounts and remove only
   those named volumes.
4. **Marker-gated removal.** Prefer the plugin Wipe (`fs.safe_rmtree` refuses any
   path outside `/www/server/javahost`) over raw `rm -rf` of the data root.
5. **Secrets from file at runtime.** Read panel/S3 creds from gitignored
   `_private_spec/OPS-ACCESS.md`; NEVER inline a secret into an ssh/docker command
   (it leaks in the remote process table — the auto-approver blocks it).
6. **Partial by default during active dev.** Use `dev-reset` (keeps runtimes/DBs)
   unless a FULL teardown is explicitly requested.

## 1. Inventory (always first, read-only)
```sh
ssh root@<box> 'set +e
 grep -o "\"versions\":[^,]*" /www/server/panel/plugin/javahost/info.json
 du -sh /www/server/javahost; ls /www/server/javahost/instances | wc -l
 ls /etc/systemd/system/javahost-*.service | wc -l
 ls /www/server/javahost/vhost/nginx/*.conf | wc -l
 ls /www/server/javahost/runtimes; ls /www/server/javahost/tomcat
 docker ps -a --format "{{.Names}} ({{.Image}})" | grep -iE "javahost|demo"
 certbot certificates | grep -E "Certificate Name|Domains"
 ls -d /root/btjdk.removed* 2>/dev/null; ls /etc/cron.d/javahost* 2>/dev/null'
```

## 2. Scope matrix (composable — pick what to remove)

| Scope | Removes | How (prefer the plugin's path) |
|---|---|---|
| `apps` | deployed test apps + their systemd units | plugin Wipe `apps` — stops each, `service.remove_unit`, marker-gated delete |
| `sites` | nginx vhosts + the JavaHost `include` line | plugin Wipe `sites` — removes vhosts + include, `nginx -t` + reload |
| `jdks` / `tomcats` | plugin runtimes / Tomcat lines (skips in-use) | plugin Wipe `jdks` / `tomcats` |
| `backups` / `remotes` / `schedules` | backups dir / storage profiles / cron schedules | `rm` backups dir; `remote.delete_profile`/rm `remotes.json`; `schedule.remove_schedule` + clear `/etc/cron.d/javahost-backups` |
| `dbs` | the 4 Docker demo DBs + their volumes | inventory mounts → `docker rm -f -v` + `docker volume rm <named vols>` |
| `data` | the whole `/www/server/javahost` root | plugin Wipe `full` (apps+sites+tomcats+jdks then the data root) |
| `plugin` | plugin code + icon + App-Store entry | `.uninstall_plan` → `install.sh uninstall`, then `rm -rf` plugin dir + `bt restart` |
| `leftovers` | `/root/btjdk.removed-*`, stray `cron.d/javahost*` | scoped `rm` |
| **`full`** | everything above | order: data → plugin → dbs → leftovers → verify |
| **`dev-reset`** | apps + sites + backups; **keeps** runtimes / Tomcats / DBs / plugin | plugin Wipe `apps,sites` + clear backups — fast redeploy loop |

The plugin's own teardown entry points (reuse, don't reinvent):
- `core/maintenance.py` → `wipe(scope, "WIPE")`, `wipe_preview()` (scopes:
  `apps,jdks,tomcats,sites,full`; stops apps first, skips in-use runtimes,
  marker-gated, removes the data root on `full`).
- `install.sh uninstall` → reads `${DATA_ROOT}/.uninstall_plan` (first line = scope
  csv) and runs `run_planned_wipe` → `maintenance.wipe`; also removes the icon.
  `PURGE=1 install.sh uninstall` instead rm's the data root directly.
- `core/backup/{remote.py,schedule.py}` for profiles/schedules.

## 3. Partial example — `dev-reset` (keep the box warm)
Drive the panel API over the authenticated session (Playwright/page-fetch), or run
`maintenance.wipe` directly on the box:
```sh
ssh root@<box> 'cd /www/server/panel/plugin/javahost && /www/server/panel/pyenv/bin/python - <<PY
import sys; sys.path.insert(0,".")
from core import maintenance
print(maintenance.wipe("apps,sites","WIPE"))   # keeps jdks/tomcats/DBs
PY
rm -f /www/server/javahost/backups/backup-*.tar.gz'
```
Then redeploy the fleet with `make matrix` / the testbed (see javahost-test-deploy).

## 4. Full teardown (when explicitly asked)
```sh
# (1) plugin-managed full wipe + icon removal
ssh root@<box> "printf 'full\n' > /www/server/javahost/.uninstall_plan && \
  bash /www/server/panel/plugin/javahost/install.sh uninstall"
# (2) remove plugin code + deregister
ssh root@<box> "rm -rf /www/server/panel/plugin/javahost && /etc/init.d/bt restart"
# (3) docker demo DBs — inventory mounts, then remove scoped
ssh root@<box> 'for c in javahost-demo-pg javahost-demo-mysql javahost-demo-maria javahost-demo-mongo; do
   docker inspect -f "{{.Name}} {{range .Mounts}}{{.Name}} {{end}}" $c 2>/dev/null; done'
ssh root@<box> 'docker rm -f -v javahost-demo-pg javahost-demo-mysql javahost-demo-maria javahost-demo-mongo
   # docker volume rm <only the named volumes listed above>'
# (4) leftovers
ssh root@<box> 'rm -rf /root/btjdk.removed-* ; rm -f /etc/cron.d/javahost-backups'
```

## 5. Verify (read-only)
`ls /www/server/javahost` → not found · `systemctl list-units 'javahost-*'` → empty ·
`ls /www/server/panel/plugin/javahost` → not found · `grep -c javahost
/www/server/nginx/conf/nginx.conf` → 0 · `nginx -t` OK · `docker ps -a | grep
javahost-demo` → empty · `certbot certificates` still lists `5d.bisotech.in`
(preserved) · panel loads · `df -h` shows the space freed.

## Out of scope
The aaPanel panel + its `5d.bisotech.in` cert; other plugins/sites/containers; the
external `*.5d.bisotech.in` DNS (remove at the provider); the GitHub repo/releases.
