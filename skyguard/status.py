"""The status file behind the status endpoint, the header indicator and the later map.

It contains no API key. It does contain the center point, because the map needs it.
"""

import json
import logging

from .feeds.base import GROUND
from .geo import from_nm
from .notifier import LOGGER_NAME
from .overlay import atomic_write_json

DISPLAY_RANGE_RADII = 2.0
PERIODIC_WRITE_S = 5.0  # matches the 5 s map refresh, and spares the SD card a write per second
MAX_EVENTS = 20
MAX_EVENT_AIRCRAFT = 10

_logger = logging.getLogger(LOGGER_NAME)


def _aircraft_entry(evaluated, settings):
    record = evaluated.record
    distance = None
    if evaluated.distance_nm is not None:
        distance = round(from_nm(evaluated.distance_nm, settings.radius_unit), 2)
    return {
        "hex": record.hex,
        "callsign": record.callsign,
        "lat": record.lat,
        "lon": record.lon,
        "alt_ft": record.alt_ft if record.alt_ft == GROUND or record.alt_ft is None else round(record.alt_ft),
        "gs_kt": record.gs_kt,
        "track_deg": record.track_deg,
        "seen_pos_s": record.seen_pos_s,
        "source": record.source,
        "distance": distance,
        "in_zone": evaluated.in_zone,
        "ground_ignored": evaluated.ground_ignored,
        "note": evaluated.note,
    }


def _in_display_range(evaluated, radius_nm):
    if evaluated.distance_nm is None:
        return True  # no position: list it, since it may be what is holding a blackout
    return evaluated.distance_nm <= radius_nm * DISPLAY_RANGE_RADII


def recent_events(events, event):
    """The list with one more on the end, the oldest dropped, so the file can't keep growing."""
    event = {**event, "aircraft": list(event["aircraft"])[:MAX_EVENT_AIRCRAFT]}
    return [*events, event][-MAX_EVENTS:]


def load_events(path):
    """The last run's events, so a restart doesn't lose them. Anything unusable means none."""
    try:
        with open(path, encoding="utf-8") as handle:
            events = json.load(handle).get("events")
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(events, list):
        return []
    usable = [e for e in events if isinstance(e, dict) and isinstance(e.get("aircraft"), list)]
    return usable[-MAX_EVENTS:]


def build_status(
    state,
    reason,
    settings,
    in_schedule,
    feed_state,
    data_age_s,
    zone,
    resolved_fixtures,
    warnings,
    updated_at,
    notices=(),
    message_test=False,
    events=(),
    held_channels=(),
):
    aircraft = zone.aircraft if zone is not None else ()
    feed = feed_state.active_feed
    radius_nm = settings.radius_nm_for(feed)
    return {
        "state": state,
        "reason": reason,
        "enabled": settings.enabled,
        "in_schedule": in_schedule,
        "source_mode": settings.source_mode,
        "active_feed": feed_state.active_feed,
        "receiver_ok": feed_state.receiver_ok,
        "last_data_age_s": None if data_age_s is None else round(data_age_s, 1),
        "aircraft_in_zone": len(zone.in_zone) if zone is not None else 0,
        "center": {"lat": settings.center_lat, "lon": settings.center_lon},
        "radius": {
            "value": settings.radius_for(feed),
            "unit": settings.radius_unit,
            "online": settings.radius,
            "receiver": settings.radius_receiver,
        },
        "aircraft": [_aircraft_entry(a, settings) for a in aircraft if _in_display_range(a, radius_nm)],
        "fixtures": [
            {
                "id": fixture.id,
                "name": fixture.name,
                "channels": [
                    {"channel": channel, "value": value, "role": role}
                    for channel, value, role in fixture.channels
                ],
                "valid": fixture.valid,
                "problem": fixture.problem,
            }
            for fixture in resolved_fixtures
        ],
        "warnings": list(warnings),
        "notices": list(notices),
        "message_test": message_test,
        "events": list(events),
        "held_channels": list(held_channels),
        "updated_at": updated_at,
    }


class StatusWriter:
    """Writes at once when something important changes, otherwise every few seconds."""

    def __init__(self, path, clock):
        self._path = path
        self._clock = clock
        self._last_write = None
        self._last_key = None

    @staticmethod
    def _key(doc):
        return (
            doc["state"],
            doc["reason"],
            doc["enabled"],
            doc["in_schedule"],
            doc["active_feed"],
            doc["aircraft_in_zone"],
            tuple(doc["warnings"]),
            doc["message_test"],
            doc["events"][-1]["at"] if doc["events"] else None,
            tuple(doc["held_channels"]),
            tuple((f["id"], f["name"], f["valid"], f["problem"]) for f in doc["fixtures"]),
        )

    def write(self, doc):
        now = self._clock()
        key = self._key(doc)
        due = self._last_write is None or now - self._last_write >= PERIODIC_WRITE_S
        if key == self._last_key and not due:
            return False
        try:
            atomic_write_json(self._path, doc)
        except OSError:
            _logger.exception("Could not write the status file")
            return False
        self._last_write = now
        self._last_key = key
        return True
