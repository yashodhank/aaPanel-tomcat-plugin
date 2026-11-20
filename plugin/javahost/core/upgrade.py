# coding: utf-8
"""Upgrade check: what an install left by an older JavaHost looks like.

This module only READS. It reports, per app, how the app's domain is served
today and what an upgrade may safely do about it. It never writes a file,
reloads nginx or restarts an app.

Run by hand:  python -m core.upgrade      (prints the report as JSON)
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from . import config
from .compat import aapanel as panel_api
from .deploy import proxy
from .deploy import ssl as sslmod
from .tomcat import instance, service, templating

_PANEL_JDK = "/usr/local/btjdk"
_TOKEN = re.compile(r"@@(\w+)@@")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(1 << 20)
    except OSError:
        return None


def unit_matches_template(text: str, template: str) -> bool:
    """Is <text> what <template> renders for SOME values? A hand-edited unit,
    or one written by an older version, does not match."""
    seen, out, pos = set(), [], 0
    for m in _TOKEN.finditer(template):
        out.append(re.escape(template[pos:m.start()]))
        name = m.group(1)
        out.append("(?P=%s)" % name if name in seen else "(?P<%s>[^\\n]*)" % name)
        seen.add(name)
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.fullmatch("".join(out), text) is not None


def _unit_state(app: str, kind: str) -> Dict:
    """{'backend', 'matches_template', 'java_home'} for the app's service file."""
    for backend, path, tmpl in (
            ("systemd", service._unit_path(app),
             "systemd-jar.service.tmpl" if kind == "jar" else "systemd.service.tmpl"),
            ("initd", service._script_path(app),
             "initd-jar.sh.tmpl" if kind == "jar" else "initd.sh.tmpl")):
        text = _read(path)
        if text is None:
            continue
        template = _read(os.path.join(templating._TEMPLATE_DIR, tmpl))
        m = re.search(r'JAVA_HOME="?([^"\n]+)"?', text)
        return {"backend": backend,
                "matches_template": (unit_matches_template(text, template)
                                     if template else None),
                "java_home": m.group(1).strip() if m else None}
    return {"backend": None, "matches_template": None, "java_home": None}


def _live(body: str) -> str:
    """The file without its comment lines."""
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


def _files_serving(names: List[str], skip=()) -> List[Dict]:
    """Every site file in the two vhost folders that names any of <names>,
    other than the paths in <skip>."""
    out = []
    names = [n.lower() for n in names]
    for vdir, legacy in ((proxy.LEGACY_VHOST_DIR, True), (proxy.VHOST_DIR, False)):
        try:
            files = sorted(os.listdir(vdir))
        except OSError:
            continue
        for f in files:
            path = os.path.join(vdir, f)
            if not f.endswith(".conf") or path in skip:
                continue
            theirs = proxy._conf_names(path)
            if not any(proxy._serves(t, n) for t in theirs for n in names):
                continue
            body = _live(_read(path) or "")
            out.append({
                "path": path,
                "ours": legacy or proxy._is_ours(path),
                "legacy": legacy,
                "https": bool(re.search(r"^\s*listen\s+[^;]*\b443\b", body, re.M)),
                "body": body,
            })
    return out


def _proxies(entry: Dict, domain: str, port: Optional[int]) -> bool:
    """Does this site file send the domain's traffic to the app's port?"""
    if not port:
        return False
    if re.search(r"proxy_pass\s+http://127\.0\.0\.1:%d\b" % port, entry["body"]):
        return True
    return (not entry["ours"]) and panel_api.site_proxies_to(domain, port)


def _own_file(path: str, legacy: bool) -> Optional[Dict]:
    body = _read(path)
    if body is None or not (legacy or proxy._is_ours(path)):
        return None
    live = _live(body)
    return {"path": path, "ours": True, "legacy": legacy, "body": live,
            "names": [n.lower() for n in proxy._conf_names(path)],
            "https": bool(re.search(r"^\s*listen\s+[^;]*\b443\b", live, re.M))}


def _owner_marker(app: str) -> Optional[str]:
    """'aapanel', 'javahost' or None. The marker sits in a directory the app's
    own user can write, so a link is not followed and nothing but the two known
    words is ever passed on."""
    path = proxy._owner_marker(app)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        word = os.read(fd, 32).decode("ascii", "replace").strip().lower()
    except OSError:
        return None
    finally:
        os.close(fd)
    return word if word in ("aapanel", "javahost") else None


def app_report(app: str, legacy_include: bool = True) -> Dict:
    base = instance.base_path(app)
    kind = instance._instance_type(base)
    port = instance._read_port(base)
    marker_domain = proxy.read_domain(app)
    owner_marker = _owner_marker(app)
    notes: List[str] = []
    attention: List[str] = []
    action = "none"
    state = "not-published"

    # The app's own site files are root-owned; the domain marker is not. The
    # files decide what the app serves, and a marker that disagrees is flagged.
    old = _own_file(proxy.legacy_vhost_path(app), True)
    cur = _own_file(proxy.vhost_path(app), False)
    own = [f for f in (old, cur) if f]
    names = sorted({n for f in own for n in f["names"]})
    domain = (names[0] if names else None) or marker_domain
    mismatch = bool(own and marker_domain and marker_domain.lower() not in names)
    if mismatch:
        attention.append("The stored domain (%s) is not the one in the app's site "
                         "file (%s)." % (marker_domain, ", ".join(names) or "none"))

    if domain:
        ask = names or [domain.lower()]
        others = _files_serving(ask, skip=[f["path"] for f in own])
        panel = [f for f in others if not f["ours"]]
        apps_ = [f for f in others if f["ours"]]
        regex = any(n.startswith("~") for n in names)
        if old and cur:
            state, action = "old-and-current-site-file", "keep"
            attention.append("The app has both an old and a current site file.")
        elif old and panel:
            state, action = "legacy-file-and-panel-website", "keep"
            attention.append(
                "%s is served by an old JavaHost site file and by a panel website "
                "(%s). Which one answers depends on load order, so the upgrade "
                "leaves both as they are." % (domain, os.path.basename(panel[0]["path"])))
        elif old:
            state = "legacy-file"
            if apps_:
                action = "keep"
                attention.append("Another JavaHost site file (%s) serves the same name."
                                 % os.path.basename(apps_[0]["path"]))
            elif regex or not names:
                action = "keep"
                attention.append("The old site file's server names could not be "
                                 "checked, so it is left where it is.")
            elif not legacy_include:
                action = "keep"
                attention.append("nginx.conf does not load the old site folder, so "
                                 "this site file is not being served.")
            else:
                action = "move"
                notes.append("The site file moves to the panel's vhost folder; the "
                             "site keeps answering the same way.")
        elif cur and (panel or apps_):
            state, action = "site-file-and-panel-website", "keep"
            attention.append("%s is also served by %s." % (
                domain, os.path.basename((panel or apps_)[0]["path"])))
        elif cur:
            state = "site-file"
        elif panel:
            state = "panel-website"
            if not any(_proxies(f, domain, port) for f in panel):
                attention.append("The panel website for %s does not send traffic "
                                 "to this app." % domain)
        else:
            state = "no-site-file"
            attention.append("%s is recorded for this app, but no nginx site "
                             "file serves it." % domain)

        if mismatch and action in ("move", "mark", "none"):
            action = "keep"
        files = own + others
        http = [f for f in files if _proxies(f, domain, port)]
        tls = [f for f in files if f["https"]]
        if sslmod.read_ssl(app) and not any(_proxies(f, domain, port) for f in tls):
            attention.append(
                "HTTPS is marked on, but no HTTPS site for %s sends traffic to "
                "this app%s." % (domain, "; plain HTTP does reach it" if http else ""))
        if owner_marker is None and action in ("none", "move") \
                and state in ("legacy-file", "site-file", "panel-website"):
            notes.append("The owner marker is missing and will be written.")
            if action == "none":
                action = "mark"

    unit = _unit_state(app, kind)
    if unit["matches_template"] is False:
        notes.append("The service file differs from the current template (edited "
                     "by hand, or written by an older version). It is left as it "
                     "is; Repair re-creates it and restarts the app.")
    jh = unit["java_home"] or ""
    if jh == _PANEL_JDK or jh.startswith(_PANEL_JDK + "/"):
        attention.append("The app runs on the panel's shared Java (%s). Install a "
                         "JavaHost runtime and repoint the app when convenient." % jh)
    if unit["backend"] is None:
        attention.append("No service file was found for this app.")

    return {"app": app, "kind": kind, "port": port, "domain": domain,
            "names": names, "owner_marker": owner_marker, "state": state,
            "action": action,
            "service": {k: unit[k] for k in ("backend", "matches_template")},
            "notes": notes, "attention": attention}


def report() -> Dict:
    """The whole install. Read-only."""
    conf = _live(_read(proxy.NGINX_CONF) or "")
    legacy_include = bool(re.search(
        r"^\s*include\s+%s/\*\.conf\s*;" % re.escape(proxy.LEGACY_VHOST_DIR), conf, re.M))
    apps = []
    try:
        names = sorted(n for n in os.listdir(instance.INSTANCE_ROOT)
                       if os.path.isdir(os.path.join(instance.INSTANCE_ROOT, n)))
    except OSError:
        names = []
    for name in names:
        try:
            apps.append(app_report(name, legacy_include))
        except Exception as e:
            apps.append({"app": name, "state": "unreadable", "action": "keep",
                         "notes": [], "attention": ["could not be read: %s" % e]})
    try:
        legacy_files = sorted(f for f in os.listdir(proxy.LEGACY_VHOST_DIR)
                              if f.endswith(".conf"))
    except OSError:
        legacy_files = []
    known = {a["app"] for a in apps}
    orphans = [f for f in legacy_files if f[:-len(".conf")] not in known]
    staying = [a["app"] for a in apps
               if a["action"] == "keep" and ("%s.conf" % a["app"]) in legacy_files]
    server = []
    if legacy_include:
        server.append(
            "nginx.conf still loads the old JavaHost site folder. %s" % (
                "The line stays while old site files remain (%s)."
                % ", ".join(staying + orphans) if (staying or orphans) else
                "The line is removed once the old site files have moved."))
    if orphans:
        server.append("Old site files with no matching app are left alone: %s."
                      % ", ".join(orphans))
    return {
        "plugin_version": config.plugin_version(),
        "publish_mode": config.publish_mode(),
        "legacy_include": legacy_include,
        "legacy_files": legacy_files,
        "apps": apps,
        "server": server,
        "counts": {
            "apps": len(apps),
            "to_move": sum(1 for a in apps if a["action"] == "move"),
            "attention": sum(1 for a in apps if a["attention"]),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(report(), indent=2, sort_keys=True))
