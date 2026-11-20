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
        template = _read(os.path.join(templating._TEMPLATE_DIR, tmpl)) or ""
        m = re.search(r'JAVA_HOME="?([^"\n]+)"?', text)
        return {"backend": backend,
                "matches_template": unit_matches_template(text, template),
                "java_home": m.group(1).strip() if m else None}
    return {"backend": None, "matches_template": None, "java_home": None}


def _files_serving(domain: str) -> List[Dict]:
    """Every site file nginx loads that names <domain>, in nginx's load order
    as far as this module can tell: legacy folder first when nginx.conf still
    includes it, then the panel's vhost folder."""
    out = []
    domain = domain.lower()
    for vdir, legacy in ((proxy.LEGACY_VHOST_DIR, True), (proxy.VHOST_DIR, False)):
        try:
            names = sorted(os.listdir(vdir))
        except OSError:
            continue
        for f in names:
            path = os.path.join(vdir, f)
            if not f.endswith(".conf"):
                continue
            body = _read(path) or ""
            if not any(proxy._serves(n, domain) for n in proxy._conf_names(path)):
                continue
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


def app_report(app: str) -> Dict:
    base = instance.base_path(app)
    kind = instance._instance_type(base)
    port = instance._read_port(base)
    domain = proxy.read_domain(app) or proxy.vhost_domain(app)
    owner_marker = (_read(proxy._owner_marker(app)) or "").strip() or None
    notes: List[str] = []
    attention: List[str] = []
    action = "none"
    state = "not-published"

    if domain:
        files = _files_serving(domain)
        ours = [f for f in files if f["ours"]]
        panel = [f for f in files if not f["ours"]]
        legacy = [f for f in ours if f["legacy"]]
        if legacy and panel:
            state = "legacy-file-and-panel-website"
            action = "keep"
            attention.append(
                "%s is served by an old JavaHost site file and by a panel website "
                "(%s). Which one answers depends on load order, so the upgrade "
                "leaves both as they are." % (domain, os.path.basename(panel[0]["path"])))
        elif legacy:
            state = "legacy-file"
            action = "move"
            notes.append("The site file moves to the panel's vhost folder; the "
                         "site keeps answering the same way.")
        elif ours and panel:
            state = "site-file-and-panel-website"
            action = "keep"
            attention.append("%s is served by a JavaHost site file and by a panel "
                             "website (%s)." % (domain, os.path.basename(panel[0]["path"])))
        elif ours:
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

        http = [f for f in files if _proxies(f, domain, port)]
        tls = [f for f in files if f["https"]]
        if sslmod.read_ssl(app) and not any(_proxies(f, domain, port) for f in tls):
            attention.append(
                "HTTPS is marked on, but no HTTPS site for %s sends traffic to "
                "this app%s." % (domain, "; plain HTTP does reach it" if http else ""))
        if owner_marker is None and state in ("legacy-file", "site-file", "panel-website"):
            notes.append("The owner marker is missing and will be written.")
            if action == "none":
                action = "mark"

    unit = _unit_state(app, kind)
    if unit["matches_template"] is False:
        notes.append("The service file differs from the current template (edited "
                     "by hand, or written by an older version). It is left as it "
                     "is; Repair re-creates it and restarts the app.")
    if (unit["java_home"] or "").startswith(_PANEL_JDK):
        attention.append("The app runs on the panel's shared Java (%s). Install a "
                         "JavaHost runtime and repoint the app when convenient."
                         % unit["java_home"])
    if unit["backend"] is None:
        attention.append("No service file was found for this app.")

    return {"app": app, "kind": kind, "port": port, "domain": domain,
            "owner_marker": owner_marker, "state": state, "action": action,
            "service": {k: unit[k] for k in ("backend", "matches_template")},
            "notes": notes, "attention": attention}


def report() -> Dict:
    """The whole install. Read-only."""
    apps = []
    try:
        names = sorted(n for n in os.listdir(instance.INSTANCE_ROOT)
                       if os.path.isdir(os.path.join(instance.INSTANCE_ROOT, n)))
    except OSError:
        names = []
    for name in names:
        try:
            apps.append(app_report(name))
        except Exception as e:
            apps.append({"app": name, "state": "unreadable", "action": "keep",
                         "notes": [], "attention": ["could not be read: %s" % e]})

    conf = _read(proxy.NGINX_CONF) or ""
    legacy_include = bool(re.search(
        r"^\s*include\s+%s/\*\.conf\s*;" % re.escape(proxy.LEGACY_VHOST_DIR), conf, re.M))
    try:
        legacy_files = sorted(f for f in os.listdir(proxy.LEGACY_VHOST_DIR)
                              if f.endswith(".conf"))
    except OSError:
        legacy_files = []
    known = {a["app"] for a in apps}
    orphans = [f for f in legacy_files if f[:-len(".conf")] not in known]
    staying = [a["app"] for a in apps
               if a["state"] == "legacy-file-and-panel-website"]
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
