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
import threading
from dataclasses import dataclass

from .fpp_api import FppError
from .notifier import LOGGER_NAME, MODEL_HELD

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
    """What the plugin holds in fppd: channels with their values, and matrix models it changed.

    The main loop and the matrix thread both change it, so every change and save happens
    under one lock. Otherwise one thread's older copy could be written last and leave out
    channels that are held, which a restarted daemon would then never release.
    """

    def __init__(self, path):
        self._path = path
        self._lock = threading.RLock()
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

    def channels_copy(self):
        with self._lock:
            return dict(self.channels)

    def matrix_models(self):
        with self._lock:
            return list(self.matrix)

    def hold_channels(self, channels):
        """Record channels before their ranges are set. Saves only if something changed."""
        with self._lock:
            if all(self.channels.get(c) == v for c, v in channels.items()):
                return True
            self.channels.update(channels)
            return self.save()

    def forget_channels(self, channels):
        """Forget channels whose ranges were deleted."""
        with self._lock:
            for channel in channels:
                self.channels.pop(channel, None)
            return self.save()

    def remember_matrix(self, model, prior_state):
        with self._lock:
            if model in self.matrix:
                return True
            self.matrix[model] = prior_state
            return self.save()

    def forget_matrix(self, model):
        with self._lock:
            self.matrix.pop(model, None)
            return self.save()

    def save(self):
        with self._lock:
            doc = {
                "channels": {str(k): v for k, v in self.channels.items()},
                "matrix": dict(self.matrix),
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
        return self._ledger.channels_copy()

    def force_resend(self):
        """Send every held range again on the next sync, as after fppd restarted and lost them."""
        self._last_sent = None
        self._sent_target = None

    def adopt_after_corrupt_ledger(self, channels):
        """With no usable record, assume the given {channel: value} are held so they get released."""
        if not self._ledger.corrupt:
            return
        self._ledger.hold_channels(channels)
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

        self._ledger.hold_channels(target)  # a failed save mustn't stop the blackout itself

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
        extra = {c: v for c, v in self._ledger.channels_copy().items() if c not in target}
        if not extra:
            return True
        ok = True
        by_value = {}
        for channel, value in extra.items():
            by_value.setdefault(value, []).append(channel)
        released = []
        for value, channels in by_value.items():
            try:
                self._api.set_ranges(channels, value)
                self._api.delete_ranges(channels)
            except FppError as error:
                _logger.warning("Could not release channel ranges: %s", error)
                ok = False
                continue
            released.extend(channels)
        self._ledger.forget_channels(released)
        return ok


@dataclass(frozen=True)
class ResolvedFixture:
    name: str
    valid: bool
    problem: str | None
    channels: tuple  # (fpp channel, value, "dimmer" or "shutter")
    id: str = ""


def universe_entries(doc, include_off=False):
    """Active universes on enabled outputs, as (first id, last id, start channel, channel count).

    include_off also lists switched-off outputs and universes, which still say where a
    universe's channels are; migrate.py uses that to turn universe rows into start channels."""
    entries = []
    outputs = doc.get("channelOutputs") if isinstance(doc, dict) else None
    for output in outputs if isinstance(outputs, list) else []:
        if not isinstance(output, dict):
            continue
        if not include_off and output.get("enabled") not in (1, "1", True):
            continue
        universes = output.get("universes")
        for universe in universes if isinstance(universes, list) else []:
            if not isinstance(universe, dict):
                continue
            if not include_off and universe.get("active", 1) in (0, "0", False):
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


MODEL_MISSING = "Model not found in FPP"


def model_entries(models):
    """FPP's channel-based models, as {name: (start channel, channel count)}.

    StartChannel is 1-based for these. Frame-buffer models report 0 and aren't heads."""
    entries = {}
    for model in models if isinstance(models, list) else []:
        if not isinstance(model, dict) or model.get("Type", "Channel") != "Channel":
            continue
        try:
            name = str(model["Name"])
            start = int(model["StartChannel"])
            count = int(model.get("ChannelCount", 512))
        except (KeyError, TypeError, ValueError):
            continue
        if start >= 1 and count >= 1:
            entries[name] = (start, count)
    return entries


def _fixture_channels(fixture, entries, models=None, last_known=None):
    """Return (list of (channel, value, role), problem).

    A head set by model follows the model if xLights re-patches it. If the model vanishes,
    the channels it last had stay held (with a problem shown), as the head is still there."""
    problem = None
    if fixture.model is not None:
        found = (models or {}).get(fixture.model)
        if found is None:
            found = (last_known or {}).get(fixture.model)
            if found is None:
                return [], MODEL_MISSING
            problem = MODEL_HELD
        base, limit = found
    elif fixture.start_channel is not None:
        base, limit = fixture.start_channel, 512
    else:
        found = _universe_base(fixture.universe, entries)
        if found is None:
            return [], "Universe not found"
        base, limit = found

    channels = []
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


def resolve_fixtures(fixtures, universe_doc, models_doc=None, last_known=None):
    """Turn fixture rows into FPP channels. Incomplete and disabled rows resolve to nothing."""
    entries = universe_entries(universe_doc)
    models = model_entries(models_doc)
    claimed = {}
    resolved = []
    for fixture in fixtures:
        if not fixture.complete:
            resolved.append(ResolvedFixture(fixture.name, False, "Incomplete", (), fixture.id))
            continue
        if not fixture.enabled:
            resolved.append(ResolvedFixture(fixture.name, True, None, (), fixture.id))
            continue
        channels, problem = _fixture_channels(fixture, entries, models, last_known)
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
    """Keeps fixtures resolved as FPP's outputs change, and reports fixtures that can't resolve.

    read_outputs() asks FPP and can block, so the daemon runs it on the FPP watch thread.
    resolve() never blocks. refresh() does both, for callers with no thread of their own.
    """

    def __init__(self, api, notifier, clock, known_path=None):
        self._api = api
        self._notifier = notifier
        self._clock = clock
        self._known_path = known_path
        self._last_known = self._load_known()
        self._lock = threading.Lock()
        self._doc = None
        self._have_doc = False
        self._doc_version = 0
        self._last_read = None
        self._read_error_logged = False
        self._fixtures = None
        self._resolved_version = None
        self._invalid = set()
        self.resolved = ()

    @property
    def ready(self):
        """Whether FPP's output configuration has been read at least once."""
        with self._lock:
            return self._have_doc

    def read_outputs(self, force=False):
        """Read FPP's output configuration if it is due, or now if forced. Returns True if read."""
        with self._lock:
            now = self._clock()
            due = force or self._last_read is None or now - self._last_read >= RESOLVE_INTERVAL_S
            if not due:
                return False
            self._last_read = now
        try:
            doc = (self._api.get_universe_outputs(), self._api.get_models())
        except FppError as error:
            with self._lock:
                # Keep resolving from the last good copy; channels don't move while FPP is away.
                if not self._have_doc:
                    self._last_read = None  # nothing to fall back on, so try again next time
                if not self._read_error_logged:
                    _logger.warning("Could not read FPP's channel outputs: %s", error)
                    self._read_error_logged = True
            return False
        with self._lock:
            if doc != self._doc or not self._have_doc:
                self._doc = doc
                self._doc_version += 1
            self._have_doc = True
            self._read_error_logged = False
        return True

    def resolve(self, fixtures):
        """Re-resolve if the fixtures or the last output configuration read changed."""
        with self._lock:
            doc, version = self._doc, self._doc_version
        if version == self._resolved_version and fixtures == self._fixtures:
            return self.resolved
        self._fixtures = fixtures
        self._resolved_version = version
        universes, models = doc if doc is not None else (None, None)
        self.resolved = resolve_fixtures(fixtures, universes, models, self._last_known)
        self._remember_models(fixtures, models)
        self._report_problems()
        return self.resolved

    # Where each head's model last was, so a model deleted or renamed in FPP doesn't
    # release a head that is still plugged in. Kept on disk for a restart in between.

    def _load_known(self):
        if not self._known_path:
            return {}
        try:
            with open(self._known_path, encoding="utf-8") as handle:
                doc = json.load(handle)
            return {str(k): (int(v[0]), int(v[1])) for k, v in doc.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, IndexError, AttributeError):
            _logger.warning("The saved model channels are unreadable; starting without them.")
            return {}

    def _remember_models(self, fixtures, models_doc):
        found = model_entries(models_doc)
        known = dict(self._last_known)
        for fixture in fixtures:
            if fixture.model is not None and fixture.model in found:
                known[fixture.model] = found[fixture.model]
        if known == self._last_known:
            return
        self._last_known = known
        if self._known_path:
            try:
                atomic_write_json(self._known_path, {k: list(v) for k, v in known.items()})
            except OSError as error:
                _logger.warning("Could not save the model channels: %s", error)

    def refresh(self, fixtures):
        self.read_outputs()
        return self.resolve(fixtures)

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
