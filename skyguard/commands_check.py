"""Whether fppd already has SkyGuard's commands as commands/descriptions.json describes them.

FPP before 10 reads plugin commands only when fppd starts, but runs each command's script
from disk, so an update needs an fppd restart only when the descriptions change. The install
script passes FPP's /api/commands answer on stdin. Exits 0 when every command is registered
as described, and 1 otherwise, including when the answer can't be read, so any doubt asks
for the restart.
"""

import json
import os
import sys

DESCRIPTIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "commands", "descriptions.json")


def _same_args(wanted, registered):
    """True if each argument has every field we describe with the same value. FPP may add fields."""
    if not isinstance(registered, list) or len(registered) != len(wanted):
        return False
    for ours, theirs in zip(wanted, registered):
        if not isinstance(theirs, dict) or any(theirs.get(key) != value for key, value in ours.items()):
            return False
    return True


def commands_registered(descriptions, registered):
    if not isinstance(registered, list):
        return False
    by_name = {entry.get("name"): entry for entry in registered if isinstance(entry, dict)}
    for command in descriptions:
        entry = by_name.get(command["name"])
        if entry is None or not _same_args(command.get("args", []), entry.get("args", [])):
            return False
    return True


def main(stdin=sys.stdin):
    try:
        with open(DESCRIPTIONS, encoding="utf-8") as handle:
            descriptions = json.load(handle)
        registered = json.loads(stdin.read())
    except (OSError, ValueError):
        return 1
    return 0 if commands_registered(descriptions, registered) else 1


if __name__ == "__main__":
    sys.exit(main())
