"""One-off data upgrades, run by fpp_install.sh as root before it hands the folder to fpp.

Run with: python3 -m skyguard.migrate

Fixture rows saved by early versions have no id, and the settings page edits rows by id.
This gives each such row one. It used to happen on the page's GET of the fixture list, but
a GET must not write.

The page now patches a head by FPP model or start channel. Rows set by universe are turned
into the start channel that universe maps to in FPP's output configuration, so they point at
the same channels. A universe FPP doesn't have is left as it is: the daemon still reads it.
"""

import json
import os
import secrets
import sys

from .config import Paths
from .overlay import _universe_base, atomic_write_json, universe_entries


def add_fixture_ids(path):
    """Give every fixture row without an id one. Returns True if the file changed."""
    try:
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
    except (OSError, ValueError):
        return False  # missing or unreadable: nothing to upgrade, and the daemon reports it
    if not isinstance(rows, list):
        return False
    changed = False
    for row in rows:
        if isinstance(row, dict) and not row.get("id"):
            row["id"] = secrets.token_hex(8)
            changed = True
    if changed:
        atomic_write_json(path, rows)
    return changed


def universes_to_start_channels(path, universes_path):
    """Turn universe rows into start-channel rows. Returns True if the file changed."""
    try:
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
        with open(universes_path, encoding="utf-8") as handle:
            entries = universe_entries(json.load(handle), include_off=True)
    except (OSError, ValueError):
        return False
    if not isinstance(rows, list):
        return False
    changed = False
    for row in rows:
        if not isinstance(row, dict) or row.get("universe") in (None, ""):
            continue
        if row.get("start_channel") not in (None, "") or row.get("model") not in (None, ""):
            continue  # an invalid row; the page shows it, and guessing could pick the wrong one
        try:
            found = _universe_base(int(row["universe"]), entries)
        except (TypeError, ValueError):
            continue
        if found is None:
            continue
        row["start_channel"] = found[0]
        row["universe"] = None
        changed = True
    if changed:
        atomic_write_json(path, rows)
    return changed


def main():
    paths = Paths.from_env()
    if os.path.exists(paths.fixtures_file):
        add_fixture_ids(paths.fixtures_file)
        universes = os.path.join(os.path.dirname(paths.config_file), "co-universes.json")
        universes_to_start_channels(paths.fixtures_file, universes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
