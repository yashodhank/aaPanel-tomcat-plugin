#!/bin/bash
# JavaHost plugin install/uninstall hook (aaPanel calls: install.sh install|uninstall)
set -euo pipefail

PLUGIN_NAME="javahost"
PANEL_PLUGIN="/www/server/panel/plugin/${PLUGIN_NAME}"
DATA_ROOT="/www/server/javahost"
# soft_ico lives under BTPanel/ on current panels; older layouts used panel/static.
ICON_DIRS=(
    "/www/server/panel/BTPanel/static/img/soft_ico"
    "/www/server/panel/static/img/soft_ico"
)

register_icon() {
    local src="${PANEL_PLUGIN}/icon.png"
    [ -f "$src" ] || return 0
    # plugins also keep ico-<name>.png in their own dir
    cp -f "$src" "${PANEL_PLUGIN}/ico-${PLUGIN_NAME}.png" 2>/dev/null || true
    local d
    for d in "${ICON_DIRS[@]}"; do
        [ -d "$d" ] && cp -f "$src" "${d}/ico-${PLUGIN_NAME}.png" 2>/dev/null || true
    done
}

install_javahost() {
    mkdir -p "${DATA_ROOT}"/{runtimes,tomcat,instances,vhost/nginx,.keys}
    chmod 700 "${DATA_ROOT}/.keys"
    register_icon
    echo "JavaHost installed. Data root: ${DATA_ROOT}"
}

# Optional plan written by the Settings UI: a csv scope (apps,jdks,tomcats,sites
# or "full"). When present, run the equivalent granular wipe via the Python
# maintenance module (typed confirm "WIPE" supplied here). DEFAULT (no plan
# file) keeps ALL data — only the plugin code/icon is removed by the panel.
UNINSTALL_PLAN="${DATA_ROOT}/.uninstall_plan"

run_planned_wipe() {
    # $1 = scope csv. Delegates to core.maintenance.wipe; defensive: any failure
    # here must NOT abort the panel's uninstall, so it never returns non-zero.
    local scope="$1"
    [ -n "$scope" ] || return 0
    local pybin
    pybin="$(command -v /www/server/panel/pyenv/bin/python 2>/dev/null \
        || command -v python3 2>/dev/null || true)"
    [ -n "$pybin" ] || { echo "no python found; skipping planned wipe"; return 0; }
    JAVAHOST_PLUGIN_DIR="${PANEL_PLUGIN}" JAVAHOST_WIPE_SCOPE="$scope" \
        "$pybin" - <<'PYEOF' || echo "planned wipe reported an error (continuing)"
import os, sys, json
sys.path.insert(0, os.environ.get("JAVAHOST_PLUGIN_DIR",
                                  "/www/server/panel/plugin/javahost"))
from core import maintenance  # noqa: E402
scope = os.environ.get("JAVAHOST_WIPE_SCOPE", "")
res = maintenance.wipe(scope, "WIPE")
print(json.dumps(res))
PYEOF
}

uninstall_javahost() {
    # Conservative DEFAULT: remove only the plugin code + icon (the panel removes
    # the code). Managed runtimes/apps under DATA_ROOT are kept unless either a
    # plan file requests a granular wipe, or the operator sets PURGE=1.
    for d in "${ICON_DIRS[@]}"; do rm -f "${d}/ico-${PLUGIN_NAME}.png" 2>/dev/null || true; done
    if [ -f "$UNINSTALL_PLAN" ]; then
        local scope
        scope="$(head -n1 "$UNINSTALL_PLAN" 2>/dev/null | tr -d '[:space:]')"
        echo "JavaHost: running planned wipe (scope=${scope})"
        run_planned_wipe "$scope"
        rm -f "$UNINSTALL_PLAN" 2>/dev/null || true
        echo "JavaHost plugin removed; planned wipe applied (scope=${scope})."
    elif [ "${PURGE:-0}" = "1" ]; then
        rm -rf "${DATA_ROOT}" 2>/dev/null || true
        echo "JavaHost purged (PURGE=1): ${DATA_ROOT} removed."
    else
        echo "JavaHost plugin removed. Runtimes/apps under ${DATA_ROOT} kept (set PURGE=1 or write ${UNINSTALL_PLAN} to remove)."
    fi
}

case "${1:-}" in
    install)   install_javahost ;;
    uninstall) uninstall_javahost ;;
    *) echo "Usage: $0 {install|uninstall}"; exit 1 ;;
esac
