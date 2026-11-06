"""Forces each head's dimmer and shutter channels through FPP overlay channel ranges.

Two rules protect the heads:

1. Set before delete, always. On FPP 9.5-10.2, deleting a range that doesn't exist
   adds it with value -1, which writes 0xFF: full on. A delete only ever follows a
   successful set of the same channel.
2. The ledger may overstate what is held but never understate it. A channel is
   recorded before its range is set and forgotten only after its delete succeeds,
   so a restarted daemon always knows what it must release.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass

from .fpp_api import FppError
from .notifier import LOGGER_NAME

HOLD_RESEND_S = 2.0
RESOLVE_INTERVAL_S = 30.0
MAX_CHANNEL = 8192 * 1024  # FPPD_MAX_CHANNELS; on 9.5 FPP doesn't check this itself

_logger = logging.getLogger(LOGGER_NAME)


def atomic_write_json(path, doc):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".skyguard-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(doc, output)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    except OSError:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


class Ledger:
    """What the plugin holds in fppd: channels with their values, and matrix models it changed."""

    def __init__(self, path):
        self._path = path
        self.channels = {}
        self.matrix = {}
        self.corrupt = False
        self._load()

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as handle:
                doc = json.load(handle)
            self.channels = {int(k): int(v) for k, v in doc["channels"].items()}
            self.matrix = {str(k): int(v) for k, v in doc["matrix"].items()}
        except FileNotFoundError:
            pass  # a first start: nothing has ever been held
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.corrupt = True
            self.channels = {}
            self.matrix = {}
            _logger.warning("The active-range ledger is unreadable.")

    @property
    def resume_blackout(self):
        """Whether a previous run may have left channels held."""
        return bool(self.channels) or self.corrupt

    def save(self):
        doc = {
            "channels": {str(k): v for k, v in self.channels.items()},
            "matrix": self.matrix,
        }
        try:
            atomic_write_json(self._path, doc)
            self.corrupt = False
            return True
        except OSError:
            _logger.exception("Could not write the active-range ledger")
            return False


class OverlayController:
    """Moves fppd's ranges to match a target of {channel: value}. Call sync() on every loop."""

    def __init__(self, api, ledger, notifier, clock):
        self._api = api
        self._ledger = ledger
        self._notifier = notifier
        self._clock = clock
        self._last_sent = None
        self._sent_target = None

    @property
    def held(self):
        return dict(self._ledger.channels)

    def adopt_after_corrupt_ledger(self, channels):
        """With no usable record, assume the given {channel: value} are held so they get released."""
        if not self._ledger.corrupt:
            return
        self._ledger.channels.update(channels)
        self._ledger.save()
        _logger.warning(
            "Releasing every current fixture channel because the ledger was unreadable. "
            "A range from a fixture removed earlier could stay until fppd restarts."
        )

    def sync(self, target):
        """Hold every target channel and release everything else. Returns True if all succeeded."""
        now = self._clock()
        ok = self._hold(target, now)
        ok = self._release_others(target) and ok
        self._notifier.emit("overlay_restored" if ok else "overlay_failed")
        return ok

    def release_all(self):
        return self.sync({})

    def _hold(self, target, now):
        if not target:
            self._sent_target = None
            return True
        changed = target != self._sent_target
        due = self._last_sent is None or now - self._last_sent >= HOLD_RESEND_S
        if not changed and not due:
            return True

        if any(self._ledger.channels.get(c) != v for c, v in target.items()):
            self._ledger.channels.update(target)
            self._ledger.save()  # a failed save mustn't stop the blackout itself

        self._last_sent = now
        ok = self._send_values(target)
        self._sent_target = dict(target) if ok else None
        return ok

    def _send_values(self, values):
        ok = True
        by_value = {}
        for channel, value in values.items():
            by_value.setdefault(value, []).append(channel)
        for value, channels in by_value.items():
            try:
                self._api.set_ranges(channels, value)
            except FppError as error:
                _logger.warning("Could not set channel ranges: %s", error)
                ok = False
        return ok

    def _release_others(self, target):
        extra = {c: v for c, v in self._ledger.channels.items() if c not in target}
        if not extra:
            return True
        ok = True
        by_value = {}
        for channel, value in extra.items():
            by_value.setdefault(value, []).append(channel)
        for value, channels in by_value.items():
            try:
                self._api.set_ranges(channels, value)
                self._api.delete_ranges(channels)
            except FppError as error:
                _logger.warning("Could not release channel ranges: %s", error)
                ok = False
                continue
            for channel in channels:
                del self._ledger.channels[channel]
        self._ledger.save()
        return ok


@dataclass(frozen=True)
class ResolvedFixture:
    name: str
    valid: bool
    problem: str | None
    channels: tuple  # (fpp channel, value, "dimmer" or "shutter")
    id: str = ""


def universe_entries(doc):
    """Active universes on enabled outputs, as (first id, last id, start channel, channel count)."""
    entries = []
    outputs = doc.get("channelOutputs") if isinstance(doc, dict) else None
    for output in outputs if isinstance(outputs, list) else []:
        if not isinstance(output, dict) or output.get("enabled") not in (1, "1", True):
            continue
        universes = output.get("universes")
        for universe in universes if isinstance(universes, list) else []:
            if not isinstance(universe, dict) or universe.get("active", 1) in (0, "0", False):
                continue
            try:
                first = int(universe["id"])
                count = max(1, int(universe.get("universeCount", 1)))
                entries.append(
                    (first, first + count - 1, int(universe["startChannel"]), int(universe["channelCount"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
    return entries


def _universe_base(universe, entries):
    """Return (channel 1 of the universe, channels per universe), or None if it isn't configured."""
    matches = [entry for entry in entries if entry[0] <= universe <= entry[1]]
    if not matches:
        return None
    if len(matches) > 1:
        _logger.warning("Universe %s is on more than one output; using the first.", universe)
    first, _last, start, count = matches[0]
    return start + (universe - first) * count, count


def _fixture_channels(fixture, entries):
    """Return (list of (channel, value, role), problem)."""
    if fixture.start_channel is not None:
        base, limit = fixture.start_channel, 512
    else:
        found = _universe_base(fixture.universe, entries)
        if found is None:
            return [], "Universe not found"
        base, limit = found

    channels = []
    problem = None
    for role, dmx, value in (
        ("dimmer", fixture.dimmer_ch, fixture.dimmer_off),
        ("shutter", fixture.shutter_ch, fixture.shutter_off),
    ):
        if dmx is None:
            continue
        channel = base + dmx - 1
        # Past the end of its universe, the formula would land on some other prop's channel.
        if dmx > limit or not 1 <= channel <= MAX_CHANNEL:
            problem = f"{role} channel is outside its output"
            continue
        channels.append((channel, value, role))
    return channels, problem


def resolve_fixtures(fixtures, universe_doc):
    """Turn fixture rows into FPP channels. Incomplete and disabled rows resolve to nothing."""
    entries = universe_entries(universe_doc)
    claimed = {}
    resolved = []
    for fixture in fixtures:
        if not fixture.complete:
            resolved.append(ResolvedFixture(fixture.name, False, "Incomplete", (), fixture.id))
            continue
        if not fixture.enabled:
            resolved.append(ResolvedFixture(fixture.name, True, None, (), fixture.id))
            continue
        channels, problem = _fixture_channels(fixture, entries)
        kept = []
        for channel, value, role in channels:
            if claimed.get(channel, value) != value:
                problem = f"{role} channel is also used by another head with a different off value"
                continue
            claimed[channel] = value
            kept.append((channel, value, role))
        resolved.append(ResolvedFixture(fixture.name, problem is None, problem, tuple(kept), fixture.id))
    return tuple(resolved)


def blackout_target(resolved):
    """The {channel: value} to hold during a blackout."""
    return {channel: value for fixture in resolved for channel, value, _role in fixture.channels}


class ChannelResolver:
    """Keeps fixtures resolved as FPP's outputs change, and reports fixtures that can't resolve."""

    def __init__(self, api, notifier, clock):
        self._api = api
        self._notifier = notifier
        self._clock = clock
        self._doc = None
        self._have_doc = False
        self._last_read = None
        self._read_error_logged = False
        self._fixtures = None
        self._invalid = set()
        self.resolved = ()

    @property
    def ready(self):
        """Whether FPP's output configuration has been read at least once."""
        return self._have_doc

    def refresh(self, fixtures):
        """Re-resolve if the fixtures changed or the output configuration is due a re-read."""
        now = self._clock()
        due = self._last_read is None or now - self._last_read >= RESOLVE_INTERVAL_S
        doc_changed = False
        if due:
            self._last_read = now
            try:
                doc = self._api.get_universe_outputs()
                doc_changed = doc != self._doc or not self._have_doc
                self._doc = doc
                self._have_doc = True
                self._read_error_logged = False
            except FppError as error:
                # Keep resolving from the last good copy; channels don't move while FPP is away.
                if not self._have_doc:
                    self._last_read = None  # nothing to fall back on, so try again next loop
                if not self._read_error_logged:
                    _logger.warning("Could not read FPP's channel outputs: %s", error)
                    self._read_error_logged = True

        if not doc_changed and fixtures == self._fixtures:
            return self.resolved
        self._fixtures = fixtures
        self.resolved = resolve_fixtures(fixtures, self._doc)
        self._report_problems()
        return self.resolved

    def _report_problems(self):
        invalid = {
            fixture.name: fixture.problem
            for fixture in self.resolved
            if not fixture.valid and fixture.problem != "Incomplete"
        }
        for name, problem in invalid.items():
            self._notifier.emit("fixture_invalid", {"fixture": name, "problem": problem})
        for name in self._invalid - set(invalid):
            self._notifier.emit("fixture_resolved", {"fixture": name})
        self._invalid = set(invalid)
