# Documentation index

JavaHost docs split into two places on purpose:

| Location | Purpose |
|----------|---------|
| [`plugin/javahost/docs/`](../plugin/javahost/docs/) | **Authoritative operator manuals** shipped in the plugin ZIP and served by the panel **Help** tab (`GetDoc`) |
| [`docs/`](.) (this tree) | Project / developer references (architecture, endpoints, testing) plus **stubs** that point at the Help manuals so GitHub README links stay stable |

## Operator manuals (Help tab)

Open these for day-to-day panel use (or via **Help** in the plugin UI):

- [User guide](../plugin/javahost/docs/user-guide.md) — install, runtimes, apps, WAR/JAR, proxy, SSL, Activity, Settings
- [WAR packaging & Spring Boot deploy](../plugin/javahost/docs/war-packaging-and-deploy.md)
- [Connecting Java apps to databases](../plugin/javahost/docs/databases-java-apps.md)
- [Backup, restore & remote storage](../plugin/javahost/docs/backup-restore.md)
- [System hardening](../plugin/javahost/docs/system-hardening.md)
- [Single-host vs multi-server](../plugin/javahost/docs/single-vs-multi-mode.md)
- [Troubleshooting](../plugin/javahost/docs/troubleshooting.md)

Screenshots for the user guide live in [`images/`](images/).

## Developer / project references

- [Architecture](architecture.md)
- [Endpoint reference](endpoints.md)
- [Java runtime](java-runtime.md)
- [Tomcat 10.1](tomcat-10.md) · [Tomcat 11](tomcat-11.md)
- [Testing runbook](testing.md) · [Test campaign](testbed.md)
- [WAR packaging (GitHub copy)](war-packaging-and-deploy.md) — same content as the Help twin
- [aaPanel plugin packaging](aaPanel-plugin-packaging.md)

## Why stubs exist under `docs/`

In v0.28.x the six operator manuals were de-duplicated: the shipped copies under
`plugin/javahost/docs/` became authoritative and the root `docs/` copies were
removed. README / INSTALL still linked to `docs/user-guide.md` etc., which
404'd on GitHub. The stub files restore those paths as permanent redirects to
the Help manuals.
