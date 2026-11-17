"""One-off data upgrades, run by fpp_install.sh as root before it hands the folder to fpp.

Run with: python3 -m skyguard.migrate

Fixture rows saved by early versions have no id, and the settings page edits rows by id.
This gives each such row one. It used to happen on the page's GET of the fixture list, but
a GET must not write.

The page now patches a head by FPP model or start channel. Rows set by universe are turned
into the start channel that universe maps to in FPP's output configuration, so they point at
the same channels. A universe FPP doesn't have is left as it is: the daemon still reads it.

Zones used to be circles: a receiver radius, an online radius and one altitude band. Each
radius becomes a square box of the same size, and the band is copied to every zone, so the
settings page shows what the daemon uses.
"""

import json
import os
import secrets
import sys

from .config import SIDES, ZONE_KEYS, Paths, parse_plugin_config

try:
    import fcntl
except ImportError:  # not Linux, as when the tests run elsewhere
    fcntl = None

_LEGACY_ZONE_RADIUS = {"zone1": "radius_receiver", "online_zone": "radius"}
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


def radius_to_zones(path):
    """Add the box zone keys an older install doesn't have. Returns True if the file changed.

    Appends under FPP's own lock, so the file keeps its owner and nothing FPP writes is lost."""
    try:
        handle = open(path, "r+", encoding="utf-8")
    except OSError:
        return False
    with handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        text = handle.read()
        raw = parse_plugin_config(text)
        added = {}
        for _zone_id, prefix, _feed, _label in ZONE_KEYS:
            keys = [f"{prefix}_{side}" for side in SIDES]
            legacy = _LEGACY_ZONE_RADIUS.get(prefix)
            if legacy and raw.get(legacy, "").strip() and not any(key in raw for key in keys):
                added.update({key: raw[legacy].strip() for key in keys})
            for limit in ("alt_floor_ft", "alt_ceiling_ft"):
                key = f"{prefix}_{limit}"
                if key not in raw and raw.get(limit, "").strip():
                    added[key] = raw[limit].strip()
        if not added:
            return False
        lines = "".join(f'{key} = "{value}"\n' for key, value in added.items())
        handle.seek(0, os.SEEK_END)
        handle.write(("" if not text or text.endswith("\n") else "\n") + lines)
    return True


def main():
    paths = Paths.from_env()
    if os.path.exists(paths.config_file):
        radius_to_zones(paths.config_file)
    if os.path.exists(paths.fixtures_file):
        add_fixture_ids(paths.fixtures_file)
        universes = os.path.join(os.path.dirname(paths.config_file), "co-universes.json")
        universes_to_start_channels(paths.fixtures_file, universes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
