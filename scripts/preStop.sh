#!/bin/bash
# Stops the supervisor and daemon so the daemon can release its channels.
# Worst case this delays fppd's stop by Python's start-up (up to about a second on a slow
# board) plus 2.2 s for the daemon to release and exit. It returns as soon as they exit.
: "${FPPDIR:=/opt/fpp}"
. "${FPPDIR}/scripts/common"
export MEDIADIR LOGDIR
cd "$(dirname "$(readlink -f "$0")")/.." || exit 0
python3 -m skyguard.supervisor stop
exit 0
