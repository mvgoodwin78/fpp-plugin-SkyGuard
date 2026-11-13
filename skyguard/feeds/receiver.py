"""Local receiver aircraft.json: readsb, dump1090-fa, tar1090 and dump978-fa share one schema.

A receiver on another device is read over http. A decoder on the FPP box itself is read
straight from the file it writes, so FPP's web server doesn't need changing. Only the fixed
paths below are ever opened, never a path from the settings: the daemon runs as root.
"""

import json
import os
from dataclasses import replace

from .base import FeedError, FeedSnapshot, parse_document
from .http_client import MAX_RESPONSE_BYTES, fetch_json

SOURCE_RECEIVER = "receiver"
SOURCE_RECEIVER_978 = "receiver_978"

# Stands in for receiver_url when the receiver is on this box.
LOCAL_RECEIVER = "local:this-box"

# Where decoders write aircraft.json by default. Keep in step with api.php.
LOCAL_1090_FILES = (
    "/run/readsb/aircraft.json",
    "/run/dump1090-fa/aircraft.json",
    "/run/dump1090/aircraft.json",
    "/run/dump1090-mutability/aircraft.json",
)
LOCAL_978_FILES = (
    "/run/skyaware978/aircraft.json",
    "/run/dump978-fa/aircraft.json",
)
# A 978 file this much older than the 1090 one is from a decoder that has stopped.
LOCAL_978_MAX_LAG_S = 5.0
_UNREADABLE = "the decoder's file on this box can't be read"


def read_local_file(path):
    """Read one decoder's file. Returns the parsed document, or None if it isn't there."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise FeedError(_UNREADABLE) from None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not os.path.isfile(path):
                raise FeedError(_UNREADABLE)
            body = handle.read(MAX_RESPONSE_BYTES + 1)
    except OSError:
        raise FeedError(_UNREADABLE) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise FeedError("the decoder's file is too large")
    try:
        return json.loads(body)
    except ValueError:
        raise FeedError("the decoder's file is not valid JSON") from None


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


def _newest(paths, source, read):
    """The snapshot from whichever of these files has the newest data, or None if none exist.

    A bad file left by an old decoder doesn't hide a good one. If every file there is bad,
    the first error is raised, which fails dark like any failed poll."""
    best = error = None
    for path in paths:
        try:
            doc = read(path)
            if doc is None:
                continue
            snapshot = parse_receiver(doc, source)
        except FeedError as failure:
            error = error or failure
            continue
        if best is None or snapshot.now_s > best.now_s:
            best = snapshot
    if best is None and error is not None:
        raise error
    return best


class LocalReceiverAdapter:
    """A decoder on this FPP box: reads the files it writes, 1090 MHz and 978 MHz together."""

    url = LOCAL_RECEIVER
    source = SOURCE_RECEIVER

    def __init__(self, read=read_local_file, files_1090=None, files_978=None):
        self._read = read
        self._files_1090 = LOCAL_1090_FILES if files_1090 is None else files_1090
        self._files_978 = LOCAL_978_FILES if files_978 is None else files_978

    def fetch(self):
        main = _newest(self._files_1090, SOURCE_RECEIVER, self._read)
        try:
            uat = _newest(self._files_978, SOURCE_RECEIVER_978, self._read)
        except FeedError:
            uat = None  # 978 is extra; a bad 978 file mustn't hide the 1090 data
        if main is None and uat is None:
            raise FeedError("no decoder found on this box (readsb, dump1090-fa or dump978-fa)")
        if main is None:
            return uat
        if uat is None or uat.now_s < main.now_s - LOCAL_978_MAX_LAG_S:
            return main
        return merge_snapshots(main, uat)


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
        category=best.category if best.category is not None else other.category,
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
