#!/bin/bash
# FPP deletes the plugin directory right after this, whatever it returns, and nothing
# else gets a chance to clean up. Every step tolerates "already gone", so running twice is safe.
: "${FPPDIR:=/opt/fpp}"
. "${FPPDIR}/scripts/common"
export MEDIADIR LOGDIR

PLUGIN_DIR="$(dirname "$(dirname "$(readlink -f "$0")")")"
cd "${PLUGIN_DIR}" || exit 0

# Read now, because changing the content policy below reloads the web server.
if ! VERSION_DOC="$(curl -s -m 2 http://localhost/api/fppd/version 2>/dev/null)"; then
    VERSION_DOC=""
fi
FPP_MAJOR="$(echo "${VERSION_DOC}" | grep -oE '"majorVersion": *"?[0-9]+' | grep -oE '[0-9]+$' | head -n 1 || true)"

# Stop first, then release, so nothing can set a range again afterwards.
python3 -m skyguard.supervisor stop 2>/dev/null || true
python3 -m skyguard.release 2>/dev/null || true

# Reinstall (FPP 10.4+) runs this with FPP_PLUGIN_REINSTALL=1 and keeps the user's setup.
# Anything else, including an unset value, is a real uninstall.
if [ "${FPP_PLUGIN_REINSTALL:-0}" != "1" ]; then
    rm -rf "${MEDIADIR}/plugindata/fpp-plugin-SkyGuard"
    rm -f "${MEDIADIR}/config/plugin.fpp-plugin-SkyGuard"
fi

if [ -x "${FPPDIR}/scripts/ManageApacheContentPolicy.sh" ]; then
    "${FPPDIR}/scripts/ManageApacheContentPolicy.sh" remove img-src https://tile.openstreetmap.org 2>/dev/null || true
fi

# As in fpp_install.sh: before FPP 10, removing the commands needs an fppd restart.
if [ -z "${FPP_MAJOR}" ] || [ "${FPP_MAJOR}" -lt 10 ]; then
    setSetting restartFlag 1
fi
exit 0
