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

uninstall_javahost() {
    # Conservative: remove only the plugin code + icon. Managed runtimes/apps
    # are left intact unless the operator runs uninstall with PURGE=1.
    for d in "${ICON_DIRS[@]}"; do rm -f "${d}/ico-${PLUGIN_NAME}.png" 2>/dev/null || true; done
    if [ "${PURGE:-0}" = "1" ]; then
        rm -rf "${DATA_ROOT}" 2>/dev/null || true
        echo "JavaHost purged (PURGE=1): ${DATA_ROOT} removed."
    else
        echo "JavaHost plugin removed. Runtimes/apps under ${DATA_ROOT} kept (set PURGE=1 to remove)."
    fi
}

case "${1:-}" in
    install)   install_javahost ;;
    uninstall) uninstall_javahost ;;
    *) echo "Usage: $0 {install|uninstall}"; exit 1 ;;
esac
