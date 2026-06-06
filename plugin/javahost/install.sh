#!/bin/bash
# JavaHost plugin install/uninstall hook (aaPanel calls: install.sh install|uninstall)
set -euo pipefail

PLUGIN_NAME="javahost"
PANEL_PLUGIN="/www/server/panel/plugin/${PLUGIN_NAME}"
DATA_ROOT="/www/server/javahost"
ICON_DST="/www/server/panel/static/img/soft_ico/ico-${PLUGIN_NAME}.png"

install_javahost() {
    mkdir -p "${DATA_ROOT}"/{runtimes,tomcat,instances,vhost/nginx,.keys}
    chmod 700 "${DATA_ROOT}/.keys"
    # Best-effort icon registration (kept optional; PNG only if provided).
    [ -f "${PANEL_PLUGIN}/icon.png" ] && cp -f "${PANEL_PLUGIN}/icon.png" "${ICON_DST}" 2>/dev/null || true
    echo "JavaHost installed. Data root: ${DATA_ROOT}"
}

uninstall_javahost() {
    # Conservative: remove only the plugin code + icon. Managed runtimes/apps
    # are left intact unless the operator runs uninstall with PURGE=1.
    rm -f "${ICON_DST}" 2>/dev/null || true
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
