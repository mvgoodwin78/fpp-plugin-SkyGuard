"""Zone evaluation and the IDLE / CLEAR / BLACKOUT state machine.

Nothing here touches the clock, the network or FPP. Callers pass in a monotonic
`now`, so every timing rule can be tested exactly.
"""

from dataclasses import dataclass

from .feeds.base import GROUND
from .geo import haversine_nm

IDLE = "IDLE"
CLEAR = "CLEAR"
BLACKOUT = "BLACKOUT"

REASON_AIRCRAFT = "aircraft"
REASON_NO_DATA = "no_data"
REASON_TEST = "test"
REASON_SCHEDULE = "schedule"
REASON_DISABLED = "disabled"
REASON_NOT_CONFIRMED = "not_confirmed"
REASON_NO_CENTER = "no_center"
REASON_NO_RADIUS = "no_radius"
REASON_RESUME = "resume"
REASON_CLEAR = "clear"

NOTE_GROUND_IGNORED = "ground (ignored)"
DEFAULT_GROUND_SPEED_LIMIT_KT = 40.0


@dataclass(frozen=True)
class EvaluatedAircraft:
    record: object
    distance_nm: float | None  # None when the aircraft has no position
    in_zone: bool
    ground_ignored: bool = False

    @property
    def note(self):
        """The label for status data and the map, or None."""
        return NOTE_GROUND_IGNORED if self.ground_ignored else None


@dataclass(frozen=True)
class ZoneResult:
    aircraft: tuple  # every record, as EvaluatedAircraft
    in_zone: tuple  # the subset that counts
    no_position_skipped: tuple  # would have counted, but count_no_position is off; for the log
    closest: object  # closest in-zone EvaluatedAircraft, or None
    ground_skipped: tuple = ()  # would have counted, but ignored as on the ground; for the log


def _altitude_passes(alt_ft, floor_ft, ceiling_ft):
    feet = 0.0 if alt_ft == GROUND else alt_ft
    return floor_ft <= feet <= ceiling_ft


def _closest(in_zone):
    """The nearest aircraft with a position; a position-less one only if none has a position."""
    positioned = [a for a in in_zone if a.distance_nm is not None]
    if positioned:
        return min(positioned, key=lambda a: a.distance_nm)
    return in_zone[0] if in_zone else None


def is_taxiing(record, ground_speed_limit_kt):
    """Whether an aircraft reports being on the ground and hasn't reached the speed limit.

    At the limit it counts as taking off. A ground report with no speed is omitted,
    because surface vehicles report this way and would otherwise hold a blackout.
    Without a ground report the aircraft is never omitted, however slow it is.
    """
    if record.alt_ft != GROUND:
        return False
    return record.gs_kt is None or record.gs_kt < ground_speed_limit_kt


def evaluate_zone(
    records,
    center_lat,
    center_lon,
    radius_nm,
    floor_ft,
    ceiling_ft,
    count_no_position,
    ignore_ground=False,
    ground_speed_limit_kt=DEFAULT_GROUND_SPEED_LIMIT_KT,
):
    """Apply the ground, distance, altitude and no-position rules to one poll's aircraft."""
    evaluated = []
    skipped = []
    ground_skipped = []
    for record in records:
        known_altitude = record.alt_ft is not None
        no_position_skip = False
        if record.has_position:
            distance = haversine_nm(center_lat, center_lon, record.lat, record.lon)
            # Inside the radius with no reported altitude counts, failing safe.
            altitude_ok = not known_altitude or _altitude_passes(record.alt_ft, floor_ft, ceiling_ft)
            in_zone = distance <= radius_nm and altitude_ok
        else:
            distance = None
            # With neither position nor altitude there is nothing to act on.
            altitude_ok = known_altitude and _altitude_passes(record.alt_ft, floor_ft, ceiling_ft)
            in_zone = altitude_ok and count_no_position
            no_position_skip = altitude_ok and not count_no_position

        ground = ignore_ground and is_taxiing(record, ground_speed_limit_kt)
        if ground:
            if in_zone or no_position_skip:
                ground_skipped.append(record)
            in_zone = False
        elif no_position_skip:
            skipped.append(record)
        evaluated.append(EvaluatedAircraft(record, distance, in_zone, ground))

    in_zone = tuple(a for a in evaluated if a.in_zone)
    return ZoneResult(
        tuple(evaluated),
        in_zone,
        tuple(skipped),
        _closest(in_zone),
        tuple(ground_skipped),
    )


def split_cross_check(receiver_records, online_records):
    """Which radius judges which aircraft, when the online feed is checking the receiver.

    Returns (records for the receiver radius, records for the online radius). An aircraft
    the receiver has located belongs to the receiver alone, wherever online data puts it.
    One it doesn't report, or reports with no position, goes by online data. An online
    record with no position adds nothing if the receiver already reports that aircraft.
    """
    located = {record.hex for record in receiver_records if record.has_position}
    reported = {record.hex for record in receiver_records}
    missed = tuple(
        record
        for record in online_records
        if record.hex not in located and (record.has_position or record.hex not in reported)
    )
    replaced = {record.hex for record in missed}
    return tuple(record for record in receiver_records if record.hex not in replaced), missed


def merge_zones(first, second):
    in_zone = first.in_zone + second.in_zone
    return ZoneResult(
        first.aircraft + second.aircraft,
        in_zone,
        first.no_position_skipped + second.no_position_skipped,
        _closest(in_zone),
        first.ground_skipped + second.ground_skipped,
    )


def data_is_fresh(now, last_success_at, last_poll_failed, max_data_age_s):
    """Whether feed data is usable. A max age of 0 means stale on the first failed poll."""
    if last_success_at is None:
        return False
    if max_data_age_s == 0:
        return not last_poll_failed
    return now - last_success_at <= max_data_age_s


@dataclass(frozen=True)
class Inputs:
    enabled: bool
    center_set: bool
    in_schedule: bool
    data_fresh: bool
    aircraft: tuple = ()  # in-zone AircraftRecords from this poll
    radius_set: bool = True
    personal_use_confirmed: bool = True


@dataclass(frozen=True)
class Transition:
    """A change of state, or of reason within a state. Every one of these is logged."""

    from_state: str
    to_state: str
    reason: str
    aircraft: tuple


class TriggerEngine:
    def __init__(self, release_delay_s, resume_blackout=False):
        self.release_delay_s = release_delay_s
        self.state = BLACKOUT if resume_blackout else IDLE
        self.reason = REASON_RESUME if resume_blackout else None
        self._test_until = None
        self._test_only = False  # the current blackout exists only because of a test
        self._clear_since = None  # when the zone last became empty with fresh data

    @property
    def test_active(self):
        return self._test_until is not None

    def start_test(self, now, seconds):
        """Force BLACKOUT for `seconds`, taking effect on the next update."""
        if seconds <= 0:
            return
        if self._test_until is None:
            self._test_only = self.state != BLACKOUT
        self._test_until = now + seconds

    def update(self, now, inputs):
        """Evaluate one poll. Returns a Transition if the state or reason changed, else None."""
        self._track_clear_time(now, inputs)
        target_state, target_reason = self._decide(now, inputs)

        if target_state == self.state and target_reason == self.reason:
            return None
        transition = Transition(self.state, target_state, target_reason, inputs.aircraft)
        self.state = target_state
        self.reason = target_reason
        return transition

    def _track_clear_time(self, now, inputs):
        if inputs.data_fresh and not inputs.aircraft:
            if self._clear_since is None:
                self._clear_since = now
        else:
            self._clear_since = None
            # A real problem during a test means the release delay applies after it.
            self._test_only = False

    def _decide(self, now, inputs):
        if not inputs.enabled:
            self._test_until = None
            return IDLE, REASON_DISABLED
        if not inputs.personal_use_confirmed:
            self._test_until = None
            return IDLE, REASON_NOT_CONFIRMED
        if self._test_until is not None:
            if now < self._test_until:
                return BLACKOUT, REASON_TEST
            self._test_until = None
        if not inputs.center_set:
            return IDLE, REASON_NO_CENTER
        if not inputs.radius_set:
            return IDLE, REASON_NO_RADIUS
        if not inputs.in_schedule:
            return IDLE, REASON_SCHEDULE
        if not inputs.data_fresh:
            return BLACKOUT, REASON_NO_DATA
        if inputs.aircraft:
            return BLACKOUT, REASON_AIRCRAFT
        if self.state != BLACKOUT or self._test_only:
            return CLEAR, REASON_CLEAR
        if now - self._clear_since >= self.release_delay_s:
            return CLEAR, REASON_CLEAR
        # Holding through the release delay. A reason left over from a test no longer applies.
        return BLACKOUT, REASON_CLEAR if self.reason == REASON_TEST else self.reason
