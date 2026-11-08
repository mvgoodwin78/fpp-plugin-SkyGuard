#!/bin/bash
set -e

# Safe to run repeatedly: Reinstall All Plugins and updates both run it again.
: "${FPPDIR:=/opt/fpp}"
. "${FPPDIR}/scripts/common"

if ! command -v python3 >/dev/null 2>&1; then
    echo "SkyGuard needs python3, which FPP normally includes."
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "SkyGuard needs Python 3.10 or later. FPP 9.5 and later on Debian 12 have it."
    exit 1
fi

PLUGIN_DIR="$(dirname "$(dirname "$(readlink -f "$0")")")"
DATA_DIR="${MEDIADIR}/plugindata/fpp-plugin-SkyGuard"
PRIVATE_FILE="${DATA_DIR}/private.json"

# Whether this is an update of an install that was already set up.
HAD_SETUP=0
if [ -e "${PRIVATE_FILE}" ]; then
    HAD_SETUP=1
fi

mkdir -p "${DATA_DIR}/commands"

# The API key and home coordinates live here, readable only by FPP's user and root.
if [ ! -e "${PRIVATE_FILE}" ]; then
    (umask 077; echo '{}' > "${PRIVATE_FILE}")
fi
chmod 600 "${PRIVATE_FILE}"

# Fixture rows from early versions get the ids the settings page edits them by.
(cd "${PLUGIN_DIR}" && MEDIADIR="${MEDIADIR}" python3 -m skyguard.migrate) || true

# The web pages run as fpp and the daemon as root, and both use this folder.
chown -R fpp:fpp "${DATA_DIR}" 2>/dev/null || true

# FPP 10 and later register commands live, through the callbacks script. Earlier versions
# read them only when fppd starts, so they need a restart, but only when fppd doesn't already
# have them as described: an update with the same commands runs the new scripts without one.
# If the version or the commands can't be read, ask for the restart: an extra restart is
# harmless, and missing commands aren't.
if ! VERSION_DOC="$(curl -s -m 2 http://localhost/api/fppd/version 2>/dev/null)"; then
    VERSION_DOC=""
fi
FPP_MAJOR="$(echo "${VERSION_DOC}" | grep -oE '"majorVersion": *"?[0-9]+' | grep -oE '[0-9]+$' | head -n 1 || true)"
if [ -z "${FPP_MAJOR}" ]; then
    setSetting restartFlag 1
elif [ "${FPP_MAJOR}" -lt 10 ]; then
    if ! COMMANDS_DOC="$(curl -s -m 2 http://localhost/api/commands 2>/dev/null)"; then
        COMMANDS_DOC=""
    fi
    if ! (cd "${PLUGIN_DIR}" && python3 -m skyguard.commands_check <<<"${COMMANDS_DOC}"); then
        setSetting restartFlag 1
    fi
fi

# Lets browsers load the map's background. Without it the pages show the radar plot instead,
# so a failure here is no reason to fail the install. A record of whether SkyGuard added the
# entry lets uninstall remove it only then, and leave it for whoever else needs it.
CSP_MARKER="${DATA_DIR}/csp_added"
if [ -x "${FPPDIR}/scripts/ManageApacheContentPolicy.sh" ]; then
    if CSP_OUT="$("${FPPDIR}/scripts/ManageApacheContentPolicy.sh" add img-src https://tile.openstreetmap.org 2>&1)"; then
        echo "${CSP_OUT}"
        case "${CSP_OUT}" in
            *"added under"*)
                touch "${CSP_MARKER}"
                ;;
            *)
                # Versions before this record existed added the entry without noting it.
                if [ "${HAD_SETUP}" = "1" ] && [ ! -e "${DATA_DIR}/csp_checked" ]; then
                    touch "${CSP_MARKER}"
                fi
                ;;
        esac
        touch "${DATA_DIR}/csp_checked"
    else
        echo "SkyGuard could not allow the map tiles, so its pages will show the radar plot."
    fi
fi

# An update has to reach the daemon, and FPP's quick restart doesn't run the stop and start
# hooks, so it would otherwise keep running the old code until the next full stop or reboot.
# Only if it's already running: a new install starts it through the lifecycle hook.
if [ -f "${DATA_DIR}/supervisor.pid" ]; then
    export MEDIADIR LOGDIR
    (cd "${PLUGIN_DIR}" && python3 -m skyguard.supervisor stop) || true
    /bin/bash "${PLUGIN_DIR}/scripts/skyguard_start.sh" || true
fi
