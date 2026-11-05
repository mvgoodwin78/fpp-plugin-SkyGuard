#!/bin/bash
# Returns at once; the work happens in the background.
exec /bin/bash "$(dirname "$(readlink -f "$0")")/skyguard_start.sh"
