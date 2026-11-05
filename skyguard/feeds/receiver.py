"""Local receiver aircraft.json: readsb, dump1090-fa, tar1090 and dump978-fa share one schema."""

from dataclasses import replace

from .base import FeedSnapshot, parse_document
from .http_client import fetch_json

SOURCE_RECEIVER = "receiver"
SOURCE_RECEIVER_978 = "receiver_978"


def parse_receiver(doc, source=SOURCE_RECEIVER):
    return parse_document(doc, "aircraft", source)


class ReceiverAdapter:
    """One aircraft.json URL on a receiver."""

    def __init__(self, url, source=SOURCE_RECEIVER, fetch=fetch_json):
        self.url = url
        self.source = source
        self._fetch = fetch

    def fetch(self):
        return parse_receiver(self._fetch(self.url), self.source)


def _position_rank(record):
    """Sort key: any position beats none, then the most recently updated position wins."""
    if not record.has_position:
        return (1, 0.0)
    return (0, record.seen_pos_s if record.seen_pos_s is not None else float("inf"))


def _combine(a, b):
    """Merge two records for one aircraft, keeping the better position and filling gaps."""
    best, other = (a, b) if _position_rank(a) <= _position_rank(b) else (b, a)
    return replace(
        best,
        callsign=best.callsign if best.callsign is not None else other.callsign,
        alt_ft=best.alt_ft if best.alt_ft is not None else other.alt_ft,
        gs_kt=best.gs_kt if best.gs_kt is not None else other.gs_kt,
        track_deg=best.track_deg if best.track_deg is not None else other.track_deg,
    )


def merge_snapshots(primary, secondary):
    """Combine a 1090 MHz snapshot with a 978 MHz one, by hex. `now_s` stays the primary's."""
    merged = {record.hex: record for record in primary.aircraft}
    for record in secondary.aircraft:
        existing = merged.get(record.hex)
        merged[record.hex] = record if existing is None else _combine(existing, record)
    return FeedSnapshot(
        aircraft=tuple(merged.values()),
        now_s=primary.now_s,
        skipped=primary.skipped + secondary.skipped,
    )
