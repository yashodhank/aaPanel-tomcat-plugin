#!/bin/bash
# JavaHost Tomcat CLI — thin, safe wrapper that delegates to the verified Python
# installer (download -> sha512 + gpg verify -> staged extract -> harden).
# This script intentionally contains NO download logic of its own, so integrity
# verification cannot be bypassed (closes audit F2).
#
# Usage:
#   tomcat_install.sh install   <9|10|11> [--patch X.Y.Z] [--prefer-java N] [--local /path/to.tar.gz --sha512 HEX]
#   tomcat_install.sh uninstall <9|10|11>
set -euo pipefail

PYBIN="$(command -v /www/server/panel/pyenv/bin/python || command -v python3)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACTION="${1:-}"; VERSION="${2:-}"
shift 2 || true

PATCH=""; PREFER=""; LOCAL=""; SHA=""
while [ $# -gt 0 ]; do
    case "$1" in
        --patch) PATCH="$2"; shift 2 ;;
        --prefer-java) PREFER="$2"; shift 2 ;;
        --local) LOCAL="$2"; shift 2 ;;
        --sha512) SHA="$2"; shift 2 ;;
        *) echo "unknown arg: $1"; exit 2 ;;
    esac
done

case "$VERSION" in 9|10|11) ;; *) echo "ERROR: version must be 9, 10, or 11"; exit 1 ;; esac

export JAVAHOST_PLUGIN_DIR="$HERE"
exec "$PYBIN" - "$ACTION" "$VERSION" "$PATCH" "$PREFER" "$LOCAL" "$SHA" <<'PYEOF'
import sys, os, json
sys.path.insert(0, os.environ.get("JAVAHOST_PLUGIN_DIR", "/www/server/panel/plugin/javahost"))
from core.tomcat import installer  # noqa: E402

action, version, patch, prefer, local, sha = sys.argv[1:7]
patch = patch or None
prefer = int(prefer) if prefer else None
local = local or None
sha = sha or None
if action == "install":
    res = installer.install(version, patch=patch, prefer_java=prefer,
                            local_tarball=local, local_sha512=sha)
    print(json.dumps(res))
elif action == "uninstall":
    installer.uninstall(version)
    print(json.dumps({"uninstalled": version}))
else:
    print("Usage: install|uninstall <9|10|11>"); sys.exit(1)
PYEOF
