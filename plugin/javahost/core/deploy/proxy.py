# coding: utf-8
"""
Reverse-proxy helper. Generates a plugin-owned Nginx vhost pointing at a
Tomcat instance's loopback HTTP port. Never edits another plugin's config
(closes F8) — writes only into a JavaHost-owned include dir and validates with
`nginx -t` before asking the panel to reload.
"""
from __future__ import annotations

import os
from typing import Optional

from ..util import shell, fs, validate

VHOST_DIR = "/www/server/javahost/vhost/nginx"

_TEMPLATE = """# Managed by JavaHost — instance @@app@@ ($domain). Do not edit by hand.
server {
    listen 80;
    server_name @@domain@@;
    location / {
        proxy_pass http://127.0.0.1:@@port@@;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_read_timeout 300;
    }
}
"""


def vhost_path(app: str) -> str:
    return os.path.join(VHOST_DIR, "%s.conf" % app)


def write_vhost(app: str, domain: str, port: int) -> str:
    app = validate.identifier(app, "app")
    domain = validate.domain(domain)
    port = validate.port(port)
    fs.ensure_dir(VHOST_DIR)
    body = (_TEMPLATE
            .replace("@@app@@", app)
            .replace("@@domain@@", domain)
            .replace("@@port@@", str(port)))
    path = vhost_path(app)
    fs.atomic_write(path, body, mode=0o644)
    return path


def remove_vhost(app: str) -> None:
    app = validate.identifier(app, "app")
    p = vhost_path(app)
    if os.path.exists(p):
        os.unlink(p)


def nginx_test() -> bool:
    nginx = shell.which("nginx") or "/www/server/nginx/sbin/nginx"
    rc, _, _ = shell.run([nginx, "-t"], check=False)
    return rc == 0


def include_hint() -> str:
    """Line the user adds once to nginx.conf http{} to pick up JavaHost vhosts."""
    return "include %s/*.conf;" % VHOST_DIR
