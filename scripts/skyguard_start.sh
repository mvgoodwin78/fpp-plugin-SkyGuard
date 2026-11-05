#!/bin/bash
# Starts the supervisor in the background and returns at once. Safe to call when it is
# already running: a second supervisor finds the lock taken and exits.
: "${FPPDIR:=/opt/fpp}"
. "${FPPDIR}/scripts/common"
export MEDIADIR LOGDIR
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
# The daemon opens its own log, so that it follows FPP's log rotation.
nohup python3 -m skyguard.supervisor run </dev/null >/dev/null 2>&1 &
exit 0
