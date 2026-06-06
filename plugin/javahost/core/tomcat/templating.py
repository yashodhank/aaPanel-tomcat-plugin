# coding: utf-8
"""
Tiny, dependency-free template renderer.

Uses unambiguous @@token@@ placeholders so that shell ${VAR} and XML ${...}
expressions inside templates are left untouched. Avoids requiring Jinja2 at the
panel runtime. Unknown tokens raise (fail closed) so a typo never ships a
half-rendered config.
"""
from __future__ import annotations

import os
import re
from typing import Dict

_TOKEN = re.compile(r"@@([a-zA-Z0-9_]+)@@")
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def render(text: str, mapping: Dict[str, str]) -> str:
    def repl(m):
        key = m.group(1)
        if key not in mapping:
            raise KeyError("missing template value: %s" % key)
        return str(mapping[key])
    return _TOKEN.sub(repl, text)


def render_file(name: str, mapping: Dict[str, str]) -> str:
    path = os.path.join(_TEMPLATE_DIR, name)
    with open(path, "r") as f:
        return render(f.read(), mapping)
