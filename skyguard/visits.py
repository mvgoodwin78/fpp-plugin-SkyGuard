"""The zone log: each aircraft that passes through a zone, with its callsign, altitude and
how long it was inside.

A visit starts on the first poll that counts the aircraft in a zone (inside the box and
the zone's altitude band) and ends once it hasn't counted there for VISIT_GAP_S, so one
missed poll doesn't split a visit in two. Finished visits are appended to a JSON-lines
file in plugindata, one line each; open ones are in the status file. The file keeps
MAX_AGE_DAYS and at most MAX_VISITS lines, trimmed once a day and at start.
"""

import json
import logging

from .feeds.base import GROUND
from .notifier import LOGGER_NAME
from .overlay import atomic_write_text

VISIT_GAP_S = 30.0
MAX_AGE_DAYS = 90
MAX_VISITS = 20000
TRIM_EVERY_S = 24 * 3600
MAX_OPEN = 200  # a safety cap; a zone with this many aircraft at once is not a real sky

_logger = logging.getLogger(LOGGER_NAME)


def _altitude(record):
    """The reported altitude as a whole number of feet, 'ground', or None."""
    if record.alt_ft == GROUND:
        return GROUND
    return None if record.alt_ft is None else round(record.alt_ft)


def _lowest(first, second):
    """The lower of two altitudes, where ground is lowest and None is unknown."""
    if first is None:
        return second
    if second is None:
        return first
    if GROUND in (first, second):
        return GROUND
    return min(first, second)


def _highest(first, second):
    if first is None or first == GROUND:
        return second if second is not None else first
    if second is None or second == GROUND:
        return first
    return max(first, second)


class _Visit:
    __slots__ = ("zone", "hex", "callsign", "source", "entered", "last", "last_seen", "alt_entry", "alt_min", "alt_max")

    def __init__(self, zone, record, wall, now):
        altitude = _altitude(record)
        self.zone = zone
        self.hex = record.hex
        self.callsign = record.callsign
        self.source = record.source
        self.entered = wall
        self.last = wall
        self.last_seen = now
        self.alt_entry = altitude
        self.alt_min = altitude
        self.alt_max = altitude

    def see(self, record, wall, now):
        altitude = _altitude(record)
        self.callsign = record.callsign or self.callsign
        self.last = wall
        self.last_seen = now
        self.alt_min = _lowest(self.alt_min, altitude)
        self.alt_max = _highest(self.alt_max, altitude)

    def as_dict(self):
        return {
            "zone": self.zone,
            "hex": self.hex,
            "callsign": self.callsign,
            "source": self.source,
            "entered": round(self.entered, 1),
            "left": round(self.last, 1),
            "seconds": round(self.last - self.entered),
            "alt_entry_ft": self.alt_entry,
            "alt_min_ft": self.alt_min,
            "alt_max_ft": self.alt_max,
        }


class VisitLog:
    def __init__(self, path, clock, wall_clock):
        self._path = path
        self._clock = clock
        self._wall_clock = wall_clock
        self._open = {}  # (zone id, hex) -> _Visit
        self._last_trim = None

    def update(self, zone_result, fresh):
        """Record one step. Presence counts only with fresh data: stale data still lists the
        last aircraft seen, which would keep their visits open forever."""
        now = self._clock()
        wall = self._wall_clock()
        if fresh and zone_result is not None:
            for evaluated in zone_result.aircraft:
                for zone_id in evaluated.zones:
                    key = (zone_id, evaluated.record.hex)
                    visit = self._open.get(key)
                    if visit is not None:
                        visit.see(evaluated.record, wall, now)
                    elif len(self._open) < MAX_OPEN:
                        self._open[key] = _Visit(zone_id, evaluated.record, wall, now)
        ended = [key for key, visit in self._open.items() if now - visit.last_seen > VISIT_GAP_S]
        if ended:
            self._write([self._open.pop(key) for key in ended])
        if self._last_trim is None or now - self._last_trim >= TRIM_EVERY_S:
            self._last_trim = now
            self.trim(wall)

    def open_visits(self):
        """The visits in progress, oldest first, for the status file."""
        return [visit.as_dict() for visit in sorted(self._open.values(), key=lambda v: v.entered)]

    def close_all(self):
        """At a clean stop, so the aircraft inside now aren't lost."""
        visits = list(self._open.values())
        self._open.clear()
        self._write(visits)

    def _write(self, visits):
        if not visits:
            return
        lines = "".join(json.dumps(visit.as_dict(), separators=(",", ":")) + "\n" for visit in visits)
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(lines)
        except OSError:
            _logger.exception("Could not write the zone log")

    def trim(self, wall):
        """Drop visits older than MAX_AGE_DAYS and all but the newest MAX_VISITS."""
        try:
            with open(self._path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return
        except OSError:
            _logger.exception("Could not read the zone log")
            return
        cutoff = wall - MAX_AGE_DAYS * 24 * 3600
        kept = []
        for line in lines:
            try:
                left = float(json.loads(line)["left"])
            except (ValueError, KeyError, TypeError):
                continue  # a line cut short by a power cut, or not ours
            if left >= cutoff:
                kept.append(line if line.endswith("\n") else line + "\n")
        kept = kept[-MAX_VISITS:]
        if len(kept) == len(lines) and all(line.endswith("\n") for line in lines):
            return
        try:
            atomic_write_text(self._path, "".join(kept))
        except OSError:
            _logger.exception("Could not trim the zone log")
