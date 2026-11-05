#!/bin/bash
# FPP deletes the plugin directory right after this, whatever it returns, and nothing
# else gets a chance to clean up. Every step tolerates "already gone", so running twice is safe.
: "${FPPDIR:=/opt/fpp}"
. "${FPPDIR}/scripts/common"
export MEDIADIR LOGDIR

PLUGIN_DIR="$(dirname "$(dirname "$(readlink -f "$0")")")"
cd "${PLUGIN_DIR}" || exit 0

# Stop first, then release, so nothing can set a range again afterwards.
python3 -m skyguard.supervisor stop 2>/dev/null || true
python3 -m skyguard.release 2>/dev/null || true

# Reinstall (FPP 10.4+) runs this with FPP_PLUGIN_REINSTALL=1 and keeps the user's setup.
# Anything else, including an unset value, is a real uninstall.
if [ "${FPP_PLUGIN_REINSTALL:-0}" != "1" ]; then
    rm -rf "${MEDIADIR}/plugindata/fpp-plugin-SkyGuard"
    rm -f "${MEDIADIR}/config/plugin.fpp-plugin-SkyGuard"
fi

# As in fpp_install.sh: before FPP 10, removing the commands needs an fppd restart.
FPP_MAJOR="$("${FPPBINDIR:-${FPPDIR}/src}/fpp" -V 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -n 1 | cut -d. -f1 || true)"
if [ -z "${FPP_MAJOR}" ] || [ "${FPP_MAJOR}" -lt 10 ]; then
    setSetting restartFlag 1
fi
exit 0
