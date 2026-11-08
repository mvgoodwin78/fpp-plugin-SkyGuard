"""One-off data upgrades, run by fpp_install.sh as root before it hands the folder to fpp.

Run with: python3 -m skyguard.migrate

Fixture rows saved by early versions have no id, and the settings page edits rows by id.
This gives each such row one. It used to happen on the page's GET of the fixture list, but
a GET must not write.
"""

import json
import os
import secrets
import sys

from .config import Paths
from .overlay import atomic_write_json


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


def main():
    paths = Paths.from_env()
    if os.path.exists(paths.fixtures_file):
        add_fixture_ids(paths.fixtures_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
