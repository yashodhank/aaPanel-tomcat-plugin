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
        nnp = backend == "systemd" and service.nnp_off(text)
        if nnp:                         # kept by Repair, so not a difference
            text = re.sub(r"^NoNewPrivileges=\S+\s*$", "NoNewPrivileges=true", text,
                          count=1, flags=re.M | re.I)
        return {"backend": backend,
                "matches_template": (unit_matches_template(text, template)
                                     if template else None),
                "java_home": m.group(1).strip() if m else None, "nnp_off": nnp}
    return {"backend": None, "matches_template": None, "java_home": None, "nnp_off": False}


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
    if entry["ours"]:
        return False
    if panel_api.site_proxies_to(domain, port):
        return True
    # the website's own rewrite file is part of its server block
    return bool(re.search(r"proxy_pass\s+http://127\.0\.0\.1:%d\b" % port,
                          _live(_read(panel_api._AAPANEL_REWRITE % domain) or "")))


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


def _old_folder_first() -> bool:
    """Does nginx.conf load the old site folder before the panel's?"""
    conf = _live(_read(proxy.NGINX_CONF) or "")
    first = conf.find("%s/*.conf" % proxy.LEGACY_VHOST_DIR)
    panel = conf.find("%s/*.conf" % proxy.VHOST_DIR)
    return 0 <= first < panel


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
                "(%s). %s The upgrade leaves both as they are." % (
                    domain, os.path.basename(panel[0]["path"]),
                    "nginx loads the old folder first, so the old file answers "
                    "plain HTTP; the panel website answers only what the old file "
                    "does not listen on, such as HTTPS." if _old_folder_first() else
                    "Which one answers depends on load order."))
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
                stays = _stays_put(proxy.legacy_vhost_path(app))
                if stays:
                    action = "keep"
                    notes.append("The old site file stays where it is: %s." % stays)
                else:
                    action = "move"
                    notes.append("The site file moves to the panel's vhost folder; "
                                 "the site keeps answering the same way.")
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
    if unit["nnp_off"]:
        notes.append("The service runs with NoNewPrivileges switched off (set by "
                     "hand, usually so the app can use sudo). Repair keeps it off.")
    jh = unit["java_home"] or ""
    if jh == _PANEL_JDK or jh.startswith(_PANEL_JDK + "/"):
        attention.append("The app runs on the panel's shared Java (%s). Install a "
                         "JavaHost runtime and repoint the app when convenient." % jh)
    if unit["backend"] is None:
        attention.append("No service file was found for this app.")

    https_fix = None
    if domain and state in ("legacy-file-and-panel-website", "panel-website"):
        try:
            plan = https_repair_plan(app)
            if plan.get("possible") or plan.get("applied"):
                https_fix = {"possible": bool(plan.get("possible")),
                             "applied": bool(plan.get("applied")),
                             "effect": plan.get("effect")}
            if plan.get("stale"):
                attention.append("The HTTPS repair points at a port this app no "
                                 "longer uses. Undo it, then apply it again.")
        except Exception:
            https_fix = None
    return {"app": app, "kind": kind, "port": port, "domain": domain,
            "https_fix": https_fix,
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
                "Old site files are still served from it (%s)."
                % ", ".join(staying + orphans) if (staying or orphans) else
                "The line is harmless once that folder is empty and is left in "
                "place; JavaHost does not edit nginx.conf."))
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


# --------------------------------------------------------------------------- #
# Applying the safe part of the report
# --------------------------------------------------------------------------- #
STATE_PATH = "/www/server/javahost/.upgrade.json"
JOURNAL_PATH = "/www/server/javahost/.upgrade-move.json"
BACKUP_ROOT = "/www/server/javahost/upgrade-backup"
_PROBE_PATHS = ("/", "/.javahost-upgrade-check")
# A name no site serves: what nginx answers for it shows which server is the
# default one, which a change of load order could alter.
_UNKNOWN_HOST = "javahost-upgrade-check.invalid"
_SETTLE = (1.5, 1.0)          # seconds to let nginx finish swapping workers
_BEFORE = (0.0, 1.0)          # gap between the two "before" samples
_MAX_ATTEMPTS = 3
_RETRY_AFTER = 3600
_JOB_GRACE = 900
# Only site files that listen the plain way are moved: port 80 or 443 on every
# address. A file bound to one address or another port cannot be checked from
# loopback, and `default_server` or `reuseport` depend on load order.
_PLAIN_LISTEN = re.compile(r"^(\[::\]:)?(80|443)(\s+(ssl|http2))*$")


class _Undo(Exception):
    pass


_LISTEN = re.compile(r"(?:^|(?<=[;{}]))\s*listen\s+([^;]+);", re.M)
_SOCKET = re.compile(r"^(\[::\]:)?(80|443)$")


def _listens(body: str) -> List[List[str]]:
    """Every listen directive in the file, as its list of words."""
    return [m.group(1).split() for m in _LISTEN.finditer(_live(body))]


def _plain_listens(body: str) -> bool:
    listens = _listens(body)
    return bool(listens) and all(_PLAIN_LISTEN.match(" ".join(l)) for l in listens)


def _sockets(body: str, only_default: bool = False) -> set:
    """The (address, port) pairs a file listens on in the plain way: every
    IPv4 address or every IPv6 address, port 80 or 443."""
    out = set()
    for words in _listens(body):
        m = _SOCKET.match(words[0]) if words else None
        if m and (not only_default or "default_server" in words[1:]):
            out.add(("[::]" if m.group(1) else "*", m.group(2)))
    return out


def _is_default_site(old: str) -> bool:
    """Could this old site file be what nginx answers with for names no site
    serves, on any address and port it listens on?

    nginx uses the first server it loaded for an address and port unless one
    is marked default_server for exactly that pair. The old folder is loaded
    before the panel's, in byte order of the file names. So the file is NOT
    the fallback for a pair only when an earlier old file listens on that same
    pair, or some file marks a default for it. Two apps can answer an unknown
    name identically, so this is decided from the files, not from a probe.
    Anything that cannot be worked out counts as "could be"."""
    try:
        files = sorted(f for f in os.listdir(proxy.LEGACY_VHOST_DIR) if f.endswith(".conf"))
    except OSError:
        return True
    me = os.path.basename(old)
    if me not in files or not all(f.isascii() for f in files):
        return True                    # load order is only certain for ASCII names
    conf = _live(_read(proxy.NGINX_CONF) or "")
    first = conf.find("%s/*.conf" % proxy.LEGACY_VHOST_DIR)
    panel = conf.find("%s/*.conf" % proxy.VHOST_DIR)
    if first < 0 or panel < 0 or first > panel:
        return True                    # the old folder is not loaded first
    mine = _sockets(_read(old) or "")
    earlier = set()
    for f in files[:files.index(me)]:
        earlier |= _sockets(_read(os.path.join(proxy.LEGACY_VHOST_DIR, f)) or "")
    marked = set()
    for vdir in (proxy.LEGACY_VHOST_DIR, proxy.VHOST_DIR):
        try:
            names = os.listdir(vdir)
        except OSError:
            continue
        for f in names:
            if f.endswith(".conf"):
                marked |= _sockets(_read(os.path.join(vdir, f)) or "", only_default=True)
    return bool(mine - earlier - marked)


def _stays_put(old: str) -> Optional[str]:
    """Why an old site file is not moved even though nothing shares its names,
    or None. Decided from the files alone, so the report can say it up front."""
    if not _plain_listens(_read(old) or ""):
        return ("it listens on a specific address or port, or sets default_server, "
                "so a move cannot be checked from this server")
    if not all(re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", n.lower())
               for n in proxy._conf_names(old)):
        return "it uses wildcard or special server names, which cannot be checked"
    if _is_default_site(old):
        return ("it is the site this server falls back to for unknown names and "
                "for its bare IP address; moving it would hand those to another site")
    return None


def _body_digest(data: bytes) -> str:
    """Digest of a response body with its digits left out. An app's error
    page that carries a timestamp or request id then reads the same on every
    request, while a different page, or a different app answering, does not."""
    import hashlib
    return hashlib.sha256(re.sub(rb"[0-9]+", b"", data)).hexdigest()


def _probe(host: str, https: bool, body: bool = False) -> Optional[tuple]:
    """What nginx on this server answers for <host>: per probe path the status,
    Location and content type (plus a digest of the body when asked), and for
    HTTPS the certificate presented. None when it could not be asked."""
    import hashlib
    import http.client
    import socket
    import ssl as _ssl
    out = []
    for path in _PROBE_PATHS:
        try:
            if https:
                # loopback only, to compare two answers: the certificate is
                # recorded, not judged
                ctx = _ssl._create_unverified_context()  # nosec B323 - 127.0.0.1 only, sends no data, compares two answers
                raw = socket.create_connection(("127.0.0.1", 443), 4)
                sock = ctx.wrap_socket(raw, server_hostname=host)
                cert = hashlib.sha256(sock.getpeercert(True) or b"").hexdigest()
                conn = http.client.HTTPSConnection("127.0.0.1", 443, timeout=4, context=ctx)
                conn.sock = sock
            else:
                cert = None
                conn = http.client.HTTPConnection("127.0.0.1", 80, timeout=4)
            conn.request("GET", path, headers={"Host": host,
                                               "User-Agent": "javahost-upgrade-check"})
            r = conn.getresponse()
            data = r.read(4096)
            out.append((r.status, r.getheader("Location"),
                        (r.getheader("Content-Type") or "").split(";")[0].strip(),
                        _body_digest(data) if body else None, cert))
            conn.close()
        except Exception:
            return None
    return tuple(out)


def _answers(names: List[str]) -> Optional[Dict]:
    """Answers for every name of the site and for a name no site serves. None
    when one of the site's own names cannot be asked over HTTP: then nothing
    can be proven."""
    out = {}
    for n in list(names) + [_UNKNOWN_HOST]:
        unknown = n == _UNKNOWN_HOST
        http = _probe(n, False, body=unknown)
        if http is None and not unknown:
            return None
        out[n] = (http, _probe(n, True, body=unknown))
    return out


def _stable_answers(names: List[str], wait) -> Optional[Dict]:
    """Two samples that agree, or None. An app that answers differently from
    one request to the next cannot be compared before and after."""
    import time
    time.sleep(wait[0])
    first = _answers(names)
    time.sleep(wait[1])
    second = _answers(names)
    return second if (first is not None and first == second) else None


def _write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    tmp = "%s.javahost-tmp" % path
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _journal(data: Optional[Dict]) -> None:
    import json
    if data is None:
        try:
            os.unlink(JOURNAL_PATH)
        except OSError:
            pass
        return
    _write_bytes(JOURNAL_PATH, json.dumps(data).encode("utf-8"), 0o600)


def _restore(old: str, raw: bytes, new: str) -> bool:
    """Put the old site file back and drop the moved copy. True when nginx
    accepted the restored layout."""
    ok = True
    try:
        _write_bytes(old, raw)
    except Exception:
        ok = False
    try:
        if ok and os.path.exists(new) and proxy._is_ours(new):
            os.unlink(new)
    except OSError:
        ok = False
    try:
        ok = bool(proxy.reload_nginx()) and ok
    except Exception:
        ok = False
    return ok


def _recover() -> Optional[str]:
    """Finish undoing a move that was cut short (process killed mid-way).

    The moved copy is removed only if it is still byte for byte what the move
    wrote. If the app was published again since, that newer file is the live
    site and is left alone."""
    import hashlib
    import json
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            j = json.load(f)
        old, new, copy, digest = j["old"], j["new"], j["copy"], j["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        with open(new, "rb") as f:
            current = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        current = None
    if current is not None and current != digest:
        _journal(None)
        return ("a move of %s was cut short, and the site has been published "
                "again since; the newer site file was kept" % old)
    try:
        with open(copy, "rb") as f:
            raw = f.read()
    except OSError:
        return "an interrupted move of %s could not be undone: its copy is missing" % old
    ok = _restore(old, raw, new)
    _journal(None)
    return None if ok else "an interrupted move of %s was undone, but nginx did not reload" % old


def _move_site(app: str, names: List[str], copy: str) -> Optional[str]:
    """Move one old site file into the panel's vhost folder. Returns the reason
    it was NOT moved, or None on success. On any doubt the old file stays."""
    old, new = proxy.legacy_vhost_path(app), proxy.vhost_path(app)
    try:
        if os.path.islink(old) or os.path.getsize(old) > (1 << 20):
            return "the old site file is a link or too large"
        with open(old, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "the old site file could not be read"
    if not names:
        return "the old site file names no domain"
    if os.path.exists(new):
        return "%s already exists" % new
    stays = _stays_put(old)
    if stays:
        return stays
    if not proxy.ensure_include():
        return "nginx.conf does not load %s" % proxy.VHOST_DIR
    for n in names:
        if proxy._domain_in_use(app, n):
            return "another site file also serves %s" % n
    before = _stable_answers(names, _BEFORE)
    if before is None:
        return ("%s could not be asked, or answers differently from one request "
                "to the next, so nothing could be proven" % names[0])
    moved = raw if text.startswith("# Managed by JavaHost") else (
        "# Managed by JavaHost — instance %s (%s). Do not edit by hand.\n"
        % (app, names[0])).encode("utf-8") + raw
    import hashlib
    _journal({"app": app, "old": old, "new": new, "copy": copy,
              "sha256": hashlib.sha256(moved).hexdigest()})
    try:
        os.makedirs(proxy.VHOST_DIR, exist_ok=True)
        _write_bytes(new, moved)
        os.unlink(old)
        if not proxy.reload_nginx():
            raise _Undo("nginx did not accept the moved file")
        if _stable_answers(names, _SETTLE) != before:
            raise _Undo("%s, or the server's default site, answered differently "
                        "after the move" % names[0])
    except BaseException as e:
        ok = _restore(old, raw, new)
        if ok and _stable_answers(names, _SETTLE) != before:
            ok = False
        if ok:
            _journal(None)
        why = str(e) if isinstance(e, _Undo) else "the move failed: %s" % (e or type(e).__name__)
        if not ok:
            why += (". RESTORE NOT CONFIRMED: check nginx now; the original file "
                    "is kept at %s" % copy)
        if not isinstance(e, Exception):
            raise
        return why
    _journal(None)
    return None


def _state() -> Dict:
    import json
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_run() -> Optional[Dict]:
    return _state() or None


def needed() -> bool:
    """Should the upgrade step run? True for a plugin version that has not run
    it, and for a run that left something undone, a few spaced-out times."""
    import time
    if os.path.exists(JOURNAL_PATH):
        return True                    # a move was cut short and must be undone
    st = _state()
    if st.get("plugin_version") != config.plugin_version():
        return True
    now = time.time()
    if int(st.get("attempts") or 0) >= _MAX_ATTEMPTS:
        return False
    if st.get("pending"):
        return now - float(st.get("ts") or 0) > _JOB_GRACE
    return bool(st.get("skipped")) and int(st.get("attempts") or 1) < _MAX_ATTEMPTS \
        and now - float(st.get("ts") or 0) > _RETRY_AFTER


def _save_state(out: Dict) -> None:
    import json
    try:
        _write_bytes(STATE_PATH, json.dumps(out, indent=2, sort_keys=True).encode("utf-8"), 0o600)
    except OSError:
        pass


def _work(rep: Dict):
    moves = [a for a in rep["apps"] if a["action"] == "move"]
    marks = [a for a in rep["apps"] if a["action"] in ("mark", "move")
             and a.get("owner_marker") is None]
    return moves, marks


def apply(dry_run: bool = False, once: bool = False) -> Optional[Dict]:
    """Do what the report marks as safe: move old site files that no other site
    file shares, and write missing owner markers. Never restarts an app, never
    edits nginx.conf; nginx is reloaded gracefully; a step that cannot be
    proven is skipped and reported. With once=True it does nothing unless the
    upgrade step is due."""
    import time
    if dry_run:
        moves, marks = _work(report())
        return {"dry_run": True, "moved": [a["app"] for a in moves],
                "marked": [a["app"] for a in marks], "skipped": {}}

    with proxy._publish_lock():
        prev = _state()
        note = _recover()                  # always, whoever runs first
        if once and not (note or needed() or prev.get("pending")):
            return None
        now = time.time()
        same = prev.get("plugin_version") == config.plugin_version()
        out = {"plugin_version": config.plugin_version(),
               "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               "ts": now, "dry_run": False, "moved": [], "marked": [], "skipped": {},
               "backup": None,
               # a run started from a status request was counted when it started
               "attempts": (int(prev.get("attempts") or 0) + (0 if prev.get("pending") else 1)
                            if same else 1)}
        if note:
            out["skipped"]["interrupted move"] = note
        moves, marks = _work(report())            # read under the lock
        if moves:
            if not proxy.nginx_test():
                for a in moves:
                    out["skipped"][a["app"]] = ("nginx's own config check fails, "
                                               "so nothing was changed")
                moves = []
            else:
                out["backup"] = os.path.join(
                    BACKUP_ROOT, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)))
        for a in moves:
            try:
                import shutil
                os.makedirs(out["backup"], mode=0o700, exist_ok=True)
                copy = os.path.join(out["backup"], "%s.conf" % a["app"])
                shutil.copyfile(proxy.legacy_vhost_path(a["app"]), copy)
                why = _move_site(a["app"], a.get("names") or [], copy)
            except Exception as e:
                why = str(e) or type(e).__name__
            if why:
                out["skipped"][a["app"]] = why
            else:
                out["moved"].append(a["app"])
        for a in marks:
            if a["app"] in out["skipped"]:
                continue
            owner = "aapanel" if a["state"] == "panel-website" else "javahost"
            try:
                proxy._store_owner(a["app"], owner)
                out["marked"].append(a["app"])
            except Exception as e:
                out["skipped"][a["app"]] = "owner marker not written: %s" % e
        _save_state(out)
        return out


_started = False


def start_if_needed() -> None:
    """Called on status requests after an update. Cheap, never raises and
    never does the work in the request: anything that touches nginx runs as a
    background job, visible under Activity."""
    global _started
    if _started:
        return
    try:
        if not needed():
            _started = True
            return
        import time
        moves, marks = _work(report())
        st = {"plugin_version": config.plugin_version(), "ts": time.time(),
              "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "moved": [], "marked": [], "skipped": {}, "backup": None}
        _started = True                      # at most once per panel process
        prev = _state()
        same = prev.get("plugin_version") == config.plugin_version()
        attempts = (int(prev.get("attempts") or 0) if same else 0) + 1
        if not moves and not marks and not os.path.exists(JOURNAL_PATH):
            _save_state(dict(prev if same else st, plugin_version=st["plugin_version"],
                             ts=st["ts"], attempts=attempts))
            return
        from . import jobs
        # counted before the job runs, so a job that keeps dying still stops
        _save_state(dict(prev if same else st, plugin_version=st["plugin_version"],
                         ts=st["ts"], attempts=attempts, pending=True))
        jobs.start("upgrade", "site files", jobs.python_work(
            "import json\nfrom core import upgrade\n"
            "print(json.dumps(upgrade.apply(once=True), indent=2, sort_keys=True))\n"))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# HTTPS repair for an app whose panel website has TLS but no proxy
# --------------------------------------------------------------------------- #
_FIX_BEGIN = "# JavaHost HTTPS proxy for %s: begin (remove this block to undo)"
_FIX_END = "# JavaHost HTTPS proxy: end"
_FIX_WS = """    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
"""
_FIX_BLOCK = """%(begin)s
location ^~ / {
    proxy_pass http://127.0.0.1:%(port)d;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
%(ws)s    proxy_read_timeout 300;
}
%(end)s
"""


def _direct(port: int, host: str) -> Optional[tuple]:
    """What the app itself answers on its loopback port: (status, content
    type) per probe path. None when it does not answer."""
    import http.client
    out = []
    for path in _PROBE_PATHS:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            conn.request("GET", path, headers={"Host": host,
                                               "User-Agent": "javahost-upgrade-check"})
            r = conn.getresponse()
            r.read(4096)
            out.append((r.status, (r.getheader("Content-Type") or "").split(";")[0].strip()))
            conn.close()
        except Exception:
            return None
    return tuple(out)


def _has_upgrade_map() -> bool:
    return panel_api._ws_map_in_vhost_dir() or \
        panel_api._WS_MAP_NEEDLE in _live(_read(proxy.NGINX_CONF) or "")


def _repair_target(app: str):
    """(domain, rewrite path, port in an applied block or None). The domain is
    taken only from the app's own root-owned site files, never from the marker
    the app's user can write."""
    names = sorted({n for f in (_own_file(proxy.legacy_vhost_path(app), True),
                                _own_file(proxy.vhost_path(app), False)) if f
                    for n in f["names"]})
    if not names or names[0].startswith(("~", "*", ".")):
        return None, None, None
    domain = names[0]
    rewrite = panel_api._AAPANEL_REWRITE % domain
    text = _read(rewrite) or ""
    a, b = text.find(_FIX_BEGIN % app), text.find(_FIX_END)
    applied = None
    if 0 <= a < b:
        m = re.search(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)", text[a:b])
        applied = int(m.group(1)) if m else 0
    return domain, rewrite, applied


def https_repair_plan(app: str) -> Dict:
    """Can HTTPS for <app> be pointed at the app with one block in its panel
    website's rewrite file? Read-only. {'possible': bool, 'reason'|'effect'}."""
    port = instance._read_port(instance.base_path(app))
    domain, rewrite, applied = _repair_target(app)

    def no(reason, **extra):
        return dict({"possible": False, "app": app, "domain": domain, "reason": reason}, **extra)
    if not domain:
        return no("the app has no site file of its own that names a plain domain")
    if not port:
        return no("the app has no port")
    if applied is not None:
        return no("the repair is already in place", applied=True,
                  stale=(applied != port), rewrite=rewrite)
    site = os.path.join(proxy.VHOST_DIR, "%s.conf" % domain)
    body = _read(site)
    if body is None or proxy._is_ours(site):
        return no("no panel website exists for %s" % domain)
    live = _live(body)
    if not any(_SOCKET.match(w[0]) and _SOCKET.match(w[0]).group(2) == "443" and "ssl" in w[1:]
               for w in _listens(body)):
        return no("the panel website has no HTTPS yet; switch HTTPS on first")
    if not re.search(r"^\s*include\s+%s\s*;" % re.escape(rewrite), live, re.M):
        return no("the panel website does not load %s" % rewrite)
    current = _read(rewrite)
    if current is None or os.path.islink(rewrite):
        return no("%s is missing" % rewrite)
    if current.strip():
        return no("the website already has URL rewrite rules (or notes in that "
                  "file); they are left alone")
    if re.search(r"(?:^|(?<=[;{}]))\s*location\s+(\^~\s+|=\s+)?/\s*\{", live, re.M) \
            or panel_api.site_proxies_to(domain, port) \
            or re.search(r"proxy_pass\s", live):
        return no("the panel website already has a location for / or a proxy")
    return {"possible": True, "app": app, "domain": domain, "port": port,
            "rewrite": rewrite,
            "effect": ("https://%s/ will show this app instead of the panel's "
                       "placeholder page. http://%s/ keeps answering exactly as "
                       "it does now. The app is not restarted. Undo with the "
                       "button that replaces this one, or by removing the marked "
                       "block from the website's URL rewrite rules."
                       % (domain, domain))}


def repair_https(app: str, undo: bool = False) -> Dict:
    """Point the panel website's HTTPS at the app (or take that back). Checked
    before and after: plain HTTP must answer exactly as before, and HTTPS must
    change to what the app itself answers; otherwise the file is put back."""
    import time
    with proxy._publish_lock():
        plan = https_repair_plan(app)
        if undo and not plan.get("applied"):
            return {"ok": False, "error": "there is no JavaHost repair to remove"}
        if not undo and not plan.get("possible"):
            return {"ok": False, "error": plan.get("reason")}
        domain, rewrite = plan["domain"], plan["rewrite"]
        port = instance._read_port(instance.base_path(app))
        if os.path.islink(rewrite):
            return {"ok": False, "error": "%s is a link" % rewrite}
        with open(rewrite, "rb") as f:
            raw = f.read()
        st = os.stat(rewrite)
        if undo:
            text = raw.decode("utf-8", "replace")
            a, b = text.find(_FIX_BEGIN % app), text.find(_FIX_END)
            if not 0 <= a < b:
                return {"ok": False, "error": "the block's markers were changed by hand; "
                                              "remove it under the website's URL rewrite rules"}
            new = (text[:a] + text[b + len(_FIX_END):]).strip("\n").encode("utf-8")
            new = new + b"\n" if new else b""
        else:
            new = (_FIX_BLOCK % {"begin": _FIX_BEGIN % app, "end": _FIX_END, "port": port,
                                 "ws": _FIX_WS if _has_upgrade_map() else ""}).encode("utf-8")
        if not proxy.nginx_test():
            return {"ok": False, "error": "nginx's own config check fails, so nothing was changed"}
        if not undo and _direct(port, domain) is None:
            return {"ok": False, "error": "the app is not answering on port %d; start "
                                          "it, then try again" % port}
        before = _stable_answers([domain], _BEFORE)
        tls_before = _probe(domain, True, body=True)
        if before is None:
            return {"ok": False, "error": "%s could not be asked, or its answers are "
                                          "not stable, so nothing was changed" % domain}
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        os.makedirs(os.path.join(BACKUP_ROOT, stamp), mode=0o700, exist_ok=True)
        copy = os.path.join(BACKUP_ROOT, stamp, "rewrite-%s.conf" % domain)
        _write_bytes(copy, raw, 0o600)

        def on_disk():
            try:
                with open(rewrite, "rb") as f:
                    return f.read()
            except OSError:
                return None
        # the checks above take a few seconds; the panel may have saved rules meanwhile
        if on_disk() != raw:
            return {"ok": False, "error": "the website's URL rewrite rules changed "
                                          "just now; nothing was written"}
        why = None
        try:
            _write_bytes(rewrite, new, st.st_mode & 0o777)
            os.chown(rewrite, st.st_uid, st.st_gid)
            if not proxy.reload_nginx():
                raise _Undo("nginx did not accept the change")
            after = _stable_answers([domain], _SETTLE)
            if after is None:
                raise _Undo("%s stopped answering" % domain)
            # plain HTTP, for the site and for unknown names, must be untouched
            if (after[domain][0], after[_UNKNOWN_HOST][0]) != (before[domain][0], before[_UNKNOWN_HOST][0]):
                raise _Undo("plain HTTP answered differently")
            if not undo:
                # HTTPS must now say what the app says, and must really have
                # changed: an app and a placeholder can share status and type
                app_says, tls = _direct(port, domain), _probe(domain, True, body=True)
                if tls is None or app_says is None or \
                        tuple((r[0], r[2]) for r in tls) != app_says:
                    raise _Undo("HTTPS still does not answer what the app answers")
                if tls_before is not None and \
                        [(r[0], r[2], r[3]) for r in tls] == [(r[0], r[2], r[3]) for r in tls_before]:
                    raise _Undo("HTTPS answers exactly what it did before, so the "
                                "change cannot be confirmed")
        except BaseException as e:
            ok = True
            if on_disk() == new:               # never overwrite what someone else saved since
                try:
                    _write_bytes(rewrite, raw, st.st_mode & 0o777)
                    os.chown(rewrite, st.st_uid, st.st_gid)
                    ok = bool(proxy.reload_nginx())
                except Exception:
                    ok = False
            else:
                ok = False
            if not isinstance(e, Exception):
                raise
            why = (str(e) if isinstance(e, _Undo) else "the change failed: %s" % e) + (
                "; the file was put back" if ok else
                ". RESTORE NOT CONFIRMED: check nginx now; the original is at %s" % copy)
        if why:
            return {"ok": False, "error": why, "backup": copy}
        return {"ok": True, "app": app, "domain": domain, "undone": bool(undo),
                "backup": copy, "url": "https://%s/" % domain}


if __name__ == "__main__":
    import json
    import sys
    if "--apply" in sys.argv:
        print(json.dumps(apply(), indent=2, sort_keys=True))
    elif "--plan" in sys.argv:
        print(json.dumps(apply(dry_run=True), indent=2, sort_keys=True))
    else:
        print(json.dumps(report(), indent=2, sort_keys=True))
