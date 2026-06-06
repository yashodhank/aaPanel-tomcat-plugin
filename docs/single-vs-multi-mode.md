# Single-host vs. multi-server deployments

JavaHost is **host-local**. Every Tomcat instance and JAR it manages runs on the
**same host where the plugin executes**. There is no agent, broker, or
controller that schedules Java apps onto other machines. If you want a Java app
to run on a given host, JavaHost must be installed on that host.

## Standalone aaPanel host

The common case: a single aaPanel server.

- Install JavaHost once, on that server.
- It installs runtimes, deploys WARs/JARs, registers services, allocates ports,
  and renders reverse-proxy vhosts — all on that one machine.
- All managed apps run locally; nothing reaches out to other hosts.

## aaPanel multi-server / node deployments

aaPanel can manage **remote nodes** from one control panel. JavaHost does **not**
piggyback on that channel. The panel's node management is for the panel's own
features; it does not turn JavaHost into a cluster scheduler.

To run Java apps across several nodes:

- **Install JavaHost on each host** that should run Java apps. A node without the
  plugin installed cannot run JavaHost-managed apps.
- Each installation is **independent**. There is no shared state, no cross-node
  coordination, and no "deploy to all nodes" action.
- Managing an app on Node A has no effect on Node B.

## What is per-host (no cluster-wide assumptions)

Everything JavaHost tracks is scoped to the local host:

- **Ports** — allocated per host. There is **no cross-node port registry**; the
  same port number can be in use on different nodes independently, and JavaHost
  only checks/reserves ports on the host it runs on.
- **Instances** — each Tomcat/JAR instance is local to its host.
- **Vhosts / reverse proxy** — the generated Nginx vhost proxies to a
  **local** upstream (e.g. `127.0.0.1:<port>`). JavaHost does not create proxies
  that point at other nodes.
- **No cluster assumptions** — no leader election, no shared session store, no
  service discovery, no replication. If you need those, layer them on yourself
  (e.g. an external load balancer in front of per-node apps).

## Where data lives

On every host, JavaHost keeps its data under:

```
/www/server/javahost
```

This includes its `config.json`, per-app state, rendered units/vhosts, and
managed markers. Because each host has its own `/www/server/javahost`, the
installations stay fully isolated from one another.

## Practical guidance

- Treat each host as a self-contained JavaHost deployment.
- For multi-node Java workloads, install and configure JavaHost on each node, and
  put a separate load balancer in front if you need traffic spread or failover.
- Do not assume an app, port, or vhost created on one node exists on another —
  it does not.
