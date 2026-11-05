"""The normalized aircraft record, and parsing shared by every feed format.

Parsing is strict about the document and lenient about each aircraft. A document
that can't be trusted raises FeedError, which counts as a failed poll and so
fails dark. One bad aircraft entry never discards the others.
"""

import math
from dataclasses import dataclass

GROUND = "ground"

# Above this, a timestamp can only be milliseconds: as seconds it would be past the year 5000.
_MS_TIMESTAMP_THRESHOLD = 1e11


class FeedError(Exception):
    """A feed response that can't be used. Treated as a failed poll."""


@dataclass(frozen=True)
class AircraftRecord:
    hex: str
    callsign: str | None
    lat: float | None
    lon: float | None
    alt_ft: float | str | None  # feet, GROUND, or None when not reported
    gs_kt: float | None
    track_deg: float | None
    seen_pos_s: float | None
    source: str

    @property
    def has_position(self):
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class FeedSnapshot:
    aircraft: tuple
    now_s: float  # when the feed generated this data, in seconds since the epoch
    skipped: int = 0  # entries dropped because they had no usable hex


def number(value):
    """Return value as a finite float, or None. JSON booleans don't count as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _position(entry):
    """Return (lat, lon, seen_pos_s), using lastPosition when lat/lon have aged out."""
    for candidate in (entry, entry.get("lastPosition")):
        if not isinstance(candidate, dict):
            continue
        lat = number(candidate.get("lat"))
        lon = number(candidate.get("lon"))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon, number(candidate.get("seen_pos"))
    return None, None, number(entry.get("seen_pos"))


def _altitude(value):
    if isinstance(value, str) and value.strip().lower() == GROUND:
        return GROUND
    return number(value)


def parse_entry(entry, source):
    """Normalize one aircraft entry. Returns None if it has no usable hex."""
    if not isinstance(entry, dict):
        return None
    raw_hex = entry.get("hex")
    if not isinstance(raw_hex, str) or not raw_hex.strip():
        return None
    flight = entry.get("flight")
    callsign = flight.strip() if isinstance(flight, str) else ""
    lat, lon, seen_pos = _position(entry)
    return AircraftRecord(
        hex=raw_hex.strip().lower(),
        callsign=callsign or None,
        lat=lat,
        lon=lon,
        alt_ft=_altitude(entry.get("alt_baro")),
        gs_kt=number(entry.get("gs")),
        track_deg=number(entry.get("track")),
        seen_pos_s=seen_pos,
        source=source,
    )


def parse_document(doc, list_key, source):
    """Parse a whole feed document, whose aircraft list is under `list_key`."""
    if not isinstance(doc, dict):
        raise FeedError("response is not a JSON object")
    entries = doc.get(list_key)
    if not isinstance(entries, list):
        raise FeedError(f"response has no '{list_key}' list")
    now = number(doc.get("now"))
    if now is None:
        raise FeedError("response has no usable 'now' timestamp")
    if now > _MS_TIMESTAMP_THRESHOLD:
        now /= 1000.0

    records = []
    skipped = 0
    for entry in entries:
        record = parse_entry(entry, source)
        if record is None:
            skipped += 1
        else:
            records.append(record)
    return FeedSnapshot(aircraft=tuple(records), now_s=now, skipped=skipped)
