#!/bin/bash
set -e

# Safe to run repeatedly: Reinstall All Plugins and updates both run it again.
: "${FPPDIR:=/opt/fpp}"
. "${FPPDIR}/scripts/common"

if ! command -v python3 >/dev/null 2>&1; then
    echo "SkyGuard needs python3, which FPP normally includes."
    exit 1
fi

DATA_DIR="${MEDIADIR}/plugindata/fpp-plugin-SkyGuard"
PRIVATE_FILE="${DATA_DIR}/private.json"

mkdir -p "${DATA_DIR}/commands"

# The API key and home coordinates live here, readable only by FPP's user and root.
if [ ! -e "${PRIVATE_FILE}" ]; then
    (umask 077; echo '{}' > "${PRIVATE_FILE}")
fi
chmod 600 "${PRIVATE_FILE}"

# The web pages run as fpp and the daemon as root, and both use this folder.
chown -R fpp:fpp "${DATA_DIR}" 2>/dev/null || true

# FPP 10 and later register commands live, through the callbacks script. Earlier versions
# read them only when fppd starts, so they need a restart. If the version can't be read,
# ask for the restart: an extra restart is harmless, and missing commands aren't.
FPP_MAJOR="$("${FPPBINDIR:-${FPPDIR}/src}/fpp" -V 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -n 1 | cut -d. -f1 || true)"
if [ -z "${FPP_MAJOR}" ] || [ "${FPP_MAJOR}" -lt 10 ]; then
    setSetting restartFlag 1
fi

# Lets browsers load the map's background. Without it the pages show the radar plot instead,
# so a failure here is no reason to fail the install.
if [ -x "${FPPDIR}/scripts/ManageApacheContentPolicy.sh" ]; then
    "${FPPDIR}/scripts/ManageApacheContentPolicy.sh" add img-src https://tile.openstreetmap.org \
        || echo "SkyGuard could not allow the map tiles, so its pages will show the radar plot."
fi

# An update has to reach the daemon, and FPP's quick restart doesn't run the stop and start
# hooks, so it would otherwise keep running the old code until the next full stop or reboot.
# Only if it's already running: a new install starts it through the lifecycle hook.
if [ -f "${DATA_DIR}/supervisor.pid" ]; then
    PLUGIN_DIR="$(dirname "$(dirname "$(readlink -f "$0")")")"
    export MEDIADIR LOGDIR
    (cd "${PLUGIN_DIR}" && python3 -m skyguard.supervisor stop) || true
    /bin/bash "${PLUGIN_DIR}/scripts/skyguard_start.sh" || true
fi
