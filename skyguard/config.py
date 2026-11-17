"""Settings, private values and the fixture list.

Bad values never stop the plugin. Each falls back to its default with a warning,
and warnings name the key but never repeat the value, so nothing private can
reach the log. On reload, an unreadable or corrupt file keeps the last good copy
rather than dropping protection for the heads it described.
"""

import json
import math
import os
import time
from dataclasses import dataclass, replace

try:
    import fcntl
except ImportError:  # not Linux, as when the tests run elsewhere
    fcntl = None
from urllib.parse import urlsplit

from . import REPO_NAME
from .feeds.online_adsbx_v2 import DEFAULT_PROVIDER, MAX_QUERY_RADIUS_NM, PROVIDERS
from .feeds.receiver import LOCAL_RECEIVER
from .geo import to_nm, zone_reach_nm

DEFAULT_MEDIA_DIR = "/home/fpp/media"
FEED_RECEIVER = "receiver"
FEED_ONLINE = "online"
SOURCE_MODES = ("receiver", "online", "both")
RECEIVER_LOCATIONS = ("other", "this_box")
RADIUS_UNITS = ("mi", "nm", "km")
SIDES = ("north", "south", "east", "west")
MAX_ZONE_DISTANCE = 250  # in the distance unit, per side

# The zones: (id, settings key prefix, feed, label). Receiver zone 2 is optional.
ZONE_RECEIVER_1 = "receiver1"
ZONE_RECEIVER_2 = "receiver2"
ZONE_ONLINE = "online"
ZONE_KEYS = (
    (ZONE_RECEIVER_1, "zone1", "receiver", "Receiver zone 1"),
    (ZONE_RECEIVER_2, "zone2", "receiver", "Receiver zone 2"),
    (ZONE_ONLINE, "online_zone", "online", "Online zone"),
)
# Before zones were boxes, each feed had a radius and both shared one altitude band.
_LEGACY_RADIUS = {ZONE_RECEIVER_1: "radius_receiver", ZONE_ONLINE: "radius"}
GEOCODERS = ("nominatim", "off")
MATRIX_POSITIONS = ("Center", "Right to Left", "Left to Right", "Bottom to Top", "Top to Bottom")

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")
_QUOTE_AND_SPACE = " \t\n\r\0\x0b\""
# A read that looks caught mid-write is believed only once it has stayed the same this long.
SUSPECT_READ_ACCEPT_S = 1.0


@dataclass(frozen=True)
class Paths:
    config_file: str
    data_dir: str
    fixtures_file: str
    private_file: str
    ledger_file: str
    model_channels_file: str
    status_file: str
    zone_log_file: str
    commands_dir: str
    supervisor_pid_file: str
    supervisor_lock_file: str
    daemon_pid_file: str
    log_file: str

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        media = env.get("MEDIADIR") or DEFAULT_MEDIA_DIR
        logs = env.get("LOGDIR") or os.path.join(media, "logs")
        data = os.path.join(media, "plugindata", REPO_NAME)
        return cls(
            config_file=os.path.join(media, "config", f"plugin.{REPO_NAME}"),
            data_dir=data,
            fixtures_file=os.path.join(data, "fixtures.json"),
            private_file=os.path.join(data, "private.json"),
            ledger_file=os.path.join(data, "active_ranges.json"),
            model_channels_file=os.path.join(data, "model_channels.json"),
            status_file=os.path.join(data, "status.json"),
            zone_log_file=os.path.join(data, "zone_visits.jsonl"),
            commands_dir=os.path.join(data, "commands"),
            supervisor_pid_file=os.path.join(data, "supervisor.pid"),
            supervisor_lock_file=os.path.join(data, "supervisor.lock"),
            daemon_pid_file=os.path.join(data, "daemon.pid"),
            log_file=os.path.join(logs, f"plugin-{REPO_NAME}.log"),
        )


@dataclass(frozen=True)
class Zone:
    """A box around the center point: how far it reaches north, south, east and west of
    it, in the distance unit, and the altitude band that counts inside it, in feet above
    the show."""

    id: str
    feed: str
    label: str
    north: float
    south: float
    east: float
    west: float
    alt_floor_ft: float = 0.0
    alt_ceiling_ft: float = 10000.0
    unit: str = "mi"

    def bounds_nm(self):
        """(north, south, east, west) in nautical miles."""
        return tuple(to_nm(getattr(self, side), self.unit) for side in SIDES)

    @property
    def reach_nm(self):
        """The distance to the farthest corner, which an online query must cover."""
        return zone_reach_nm(self.bounds_nm())


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    source_mode: str = "receiver"  # the local option, so nothing is sent until online is chosen
    receiver_location: str = "other"  # "this_box": read the decoder's own files on the FPP box
    receiver_url: str = ""  # LOCAL_RECEIVER when the receiver is on this box
    receiver_978_url: str = ""
    online_provider: str = DEFAULT_PROVIDER
    online_api_key: str = ""
    personal_use_confirmed: bool = False  # the plugin does nothing until this is confirmed
    poll_interval_s: float = 1.0
    online_idle_interval_s: float = 30.0
    online_between_shows: bool = False  # off: online data is only asked for during a show
    health_interval_s: float = 10.0
    health_fail_count: int = 2
    max_data_age_s: float = 5.0
    center_lat: float | None = None
    center_lon: float | None = None
    # The complete zones, as Zone. Receiver zone 1 is required with a receiver and the online
    # zone with online data; neither has a default, and neither borrows the other.
    zones: tuple = ()
    radius_unit: str = "mi"  # the distance unit, for the zones and the map range
    map_range: float = 25.0  # how far out the map and list show aircraft, in radius_unit; display only
    show_elevation_ft: float = 0.0  # the show's height above sea level; the limits are above the show
    count_no_position: bool = True
    ignore_ground: bool = False
    ground_speed_limit_kt: float = 40.0
    release_delay_s: float = 5.0
    matrix_enabled: bool = False
    matrix_models: tuple = ()
    matrix_message: str = "Aircraft overhead: {callsign} at {altitude} ft"
    matrix_nodata_message: str = "Aircraft data offline – lights paused"
    matrix_color: str = "#FF0000"
    matrix_font: str = ""  # empty means the first font FPP lists
    matrix_size: int = 18
    matrix_position: str = "Right to Left"
    matrix_speed: int = 40
    geocoder: str = "nominatim"
    zone2_enabled: bool = False  # receiver zone 2 is used only when this is on
    warnings: tuple = ()

    @property
    def center_set(self):
        return self.center_lat is not None and self.center_lon is not None

    @property
    def uses_online(self):
        return self.source_mode in ("online", "both")

    @property
    def uses_receiver(self):
        if self.source_mode == "receiver":
            return True
        return self.source_mode == "both" and bool(self.receiver_url or self.receiver_978_url)

    def zone(self, zone_id):
        for zone in self.zones:
            if zone.id == zone_id:
                return zone
        return None

    @property
    def zones_ready(self):
        """Whether the zone each data source in use needs has been entered."""
        if self.uses_online and self.zone(ZONE_ONLINE) is None:
            return False
        return not (self.uses_receiver and self.zone(ZONE_RECEIVER_1) is None)

    def zones_for(self, feed):
        """The zones that judge aircraft from `feed`."""
        return tuple(zone for zone in self.zones if zone.feed == feed)

    @property
    def map_range_nm(self):
        return to_nm(self.map_range, self.radius_unit)

    @property
    def online_reach_nm(self):
        """How far online queries must reach to cover the online zone, or None."""
        zone = self.zone(ZONE_ONLINE)
        return None if zone is None else zone.reach_nm


@dataclass(frozen=True)
class Fixture:
    name: str
    id: str = ""
    enabled: bool = True
    model: str | None = None  # an FPP model name; its start channel is looked up, and followed
    universe: int | None = None  # older rows only; the page offers a model or a start channel
    start_channel: int | None = None
    dimmer_ch: int | None = None
    dimmer_off: int = 0
    shutter_ch: int | None = None
    shutter_off: int = 0
    problems: tuple = ()  # why the row is incomplete; empty when it is complete

    @property
    def complete(self):
        return not self.problems

    @property
    def active(self):
        """Whether the trigger output should act on this head."""
        return self.enabled and self.complete


def parse_plugin_config(text):
    """Parse FPP's `key = "value"` plugin config, trimming values as FPP's own reader does."""
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;" or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip(_QUOTE_AND_SPACE)
    return values


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError


def _parse_float(value):
    if isinstance(value, bool):
        raise ValueError
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _parse_int(value):
    """Whole numbers only: 1.5 or True as a DMX channel is a mistake, not something to round."""
    result = _parse_float(value)
    if result != int(result):
        raise ValueError
    return int(result)


def _valid_http_url(value):
    """Receiver URLs are fetched by a root daemon, so only plain http(s) with a host is allowed."""
    if any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


class _Reader:
    """Reads raw values into typed settings, falling back to defaults with a warning."""

    def __init__(self, raw):
        self.raw = raw
        self.defaults = Settings()
        self.warnings = []

    def _fail(self, key, default):
        if default is None:
            self.warnings.append(f"Setting '{key}' is invalid, so it is treated as not set.")
        else:
            self.warnings.append(f"Setting '{key}' is invalid; using the default ({default}).")
        return default

    def _value(self, key, parse):
        default = getattr(self.defaults, key)
        value = self.raw.get(key)
        if _is_blank(value):
            return default
        try:
            return parse(value)
        except (TypeError, ValueError, OverflowError):
            return self._fail(key, default)

    def boolean(self, key):
        return self._value(key, _parse_bool)

    def number(self, key, low=None, high=None, parse=_parse_float):
        value = self._value(key, parse)
        if value is None:
            return None
        if (low is not None and value < low) or (high is not None and value > high):
            return self._fail(key, getattr(self.defaults, key))
        return value

    def choice(self, key, choices):
        value = self._value(key, str)
        return value if value in choices else self._fail(key, getattr(self.defaults, key))

    def text(self, key):
        # A blank message is meaningful ("Empty = none"), so only a missing key takes the default.
        return str(self.raw[key]) if key in self.raw else getattr(self.defaults, key)

    def url(self, key):
        value = str(self.raw.get(key) or "").strip()
        if value and not _valid_http_url(value):
            self.warnings.append(f"Setting '{key}' is not a valid http(s) URL and is ignored.")
            return ""
        return value


def _load_center(private, warnings):
    lat = private.get("center_lat")
    lon = private.get("center_lon")
    if _is_blank(lat) and _is_blank(lon):
        return None, None
    try:
        lat = _parse_float(lat)
        lon = _parse_float(lon)
    except (TypeError, ValueError):
        lat = lon = float("nan")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        warnings.append("The saved center point is invalid; set it again.")
        return None, None
    return lat, lon


def _band_value(read, key, legacy_key, default):
    """One altitude limit: the zone's own key, else the band all zones shared before."""
    if key not in read.raw:
        key = legacy_key
    value = read.raw.get(key)
    if _is_blank(value):
        return default
    try:
        return _parse_float(value)
    except (TypeError, ValueError):
        read.warnings.append(f"Setting '{key}' is invalid; using the default ({default:g}).")
        return default


def _altitude_band(read, prefix, label):
    floor = _band_value(read, f"{prefix}_alt_floor_ft", "alt_floor_ft", Zone.alt_floor_ft)
    ceiling = _band_value(read, f"{prefix}_alt_ceiling_ft", "alt_ceiling_ft", Zone.alt_ceiling_ft)
    if floor > ceiling:
        read.warnings.append(f"{label}: the lowest altitude is above the highest, so 0 to 10,000 feet is used.")
        return Zone.alt_floor_ft, Zone.alt_ceiling_ft
    return floor, ceiling


def _legacy_distances(read, legacy):
    """An install from before zones were boxes: its radius becomes a square box of the same
    size, so an update never drops protection. The update's migration writes the four
    distances, so this lasts only until then."""
    try:
        radius = _parse_float(read.raw[legacy])
    except (TypeError, ValueError):
        radius = 0
    if 0 < radius <= MAX_ZONE_DISTANCE:
        return (radius,) * 4
    read.warnings.append(f"Setting '{legacy}' is invalid, so it is treated as not set.")
    return None


def _zone_distances(read, zone_id, prefix, label):
    """The four distances, or None when the zone isn't complete."""
    keys = [f"{prefix}_{side}" for side in SIDES]
    legacy = _LEGACY_RADIUS.get(zone_id)
    if legacy and not any(key in read.raw for key in keys) and not _is_blank(read.raw.get(legacy)):
        return _legacy_distances(read, legacy)
    values = []
    bad = False
    for key in keys:
        value = read.raw.get(key)
        if _is_blank(value):
            values.append(None)
            continue
        try:
            number = _parse_float(value)
        except (TypeError, ValueError):
            number = -1
        if not 0 <= number <= MAX_ZONE_DISTANCE:
            read.warnings.append(f"Setting '{key}' is invalid, so it is treated as not set.")
            bad = True
            number = None
        values.append(number)
    if any(value is None for value in values):
        if not bad and any(value is not None for value in values):
            read.warnings.append(f"{label} needs all four distances: north, south, east and west.")
        return None
    north, south, east, west = values
    if north + south <= 0 or east + west <= 0:
        read.warnings.append(f"{label} has no area: north plus south, and east plus west, must each be more than 0.")
        return None
    return tuple(values)


def _load_zones(raw, unit, warnings):
    read = _Reader(raw)
    zones = []
    for zone_id, prefix, feed, label in ZONE_KEYS:
        if zone_id == ZONE_RECEIVER_2 and not read.boolean("zone2_enabled"):
            continue
        distances = _zone_distances(read, zone_id, prefix, label)
        floor, ceiling = _altitude_band(read, prefix, label)
        if distances is not None:
            zones.append(Zone(zone_id, feed, label, *distances, floor, ceiling, unit))
    warnings.extend(read.warnings)
    return tuple(zones)


def load_settings(raw, private=None):
    """Build Settings from the plugin config values and the private.json values."""
    private = private or {}
    read = _Reader(raw)

    settings = Settings(
        enabled=read.boolean("enabled"),
        source_mode=read.choice("source_mode", SOURCE_MODES),
        receiver_location=read.choice("receiver_location", RECEIVER_LOCATIONS),
        receiver_url=read.url("receiver_url"),
        receiver_978_url=read.url("receiver_978_url"),
        online_provider=read.choice("online_provider", tuple(PROVIDERS)),
        online_api_key=str(private.get("online_api_key") or ""),
        personal_use_confirmed=read.boolean("personal_use_confirmed"),
        poll_interval_s=read.number("poll_interval_s", 1, 2),
        online_idle_interval_s=read.number("online_idle_interval_s", 5, 300),
        online_between_shows=read.boolean("online_between_shows"),
        health_interval_s=read.number("health_interval_s", 1),
        health_fail_count=read.number("health_fail_count", 1, parse=_parse_int),
        max_data_age_s=read.number("max_data_age_s", 0),
        radius_unit=read.choice("radius_unit", RADIUS_UNITS),
        map_range=read.number("map_range", 1, 250),
        show_elevation_ft=read.number("show_elevation_ft", -1500, 15000),
        count_no_position=read.boolean("count_no_position"),
        ignore_ground=read.boolean("ignore_ground"),
        ground_speed_limit_kt=read.number("ground_speed_limit_kt", 5, 100),
        release_delay_s=read.number("release_delay_s", 0),
        matrix_enabled=read.boolean("matrix_enabled"),
        matrix_models=tuple(
            name.strip() for name in str(raw.get("matrix_models") or "").split(",") if name.strip()
        ),
        matrix_message=read.text("matrix_message"),
        matrix_nodata_message=read.text("matrix_nodata_message"),
        matrix_color=read.text("matrix_color"),
        matrix_font=read.text("matrix_font"),
        matrix_size=read.number("matrix_size", 4, 100, parse=_parse_int),
        matrix_position=read.choice("matrix_position", MATRIX_POSITIONS),
        matrix_speed=read.number("matrix_speed", 0, 200, parse=_parse_int),
        geocoder=read.choice("geocoder", GEOCODERS),
    )

    warnings = read.warnings
    if settings.receiver_location == "this_box":
        # 978 MHz on this box is found and merged by the local adapter, so no second feed.
        settings = replace(settings, receiver_url=LOCAL_RECEIVER, receiver_978_url="")
    settings = replace(settings, zones=_load_zones(raw, settings.radius_unit, warnings))
    reach = settings.online_reach_nm
    if reach is not None and reach > MAX_QUERY_RADIUS_NM:
        warnings.append(
            f"The online zone reaches farther than the {MAX_QUERY_RADIUS_NM} nm online feeds can query, "
            "so an online feed won't cover the whole zone."
        )
    center_lat, center_lon = _load_center(private, warnings)
    return replace(
        settings,
        center_lat=center_lat,
        center_lon=center_lon,
        warnings=tuple(warnings),
    )


def _optional_int(row, key, low, high, problems):
    value = row.get(key)
    if _is_blank(value):
        return None
    try:
        result = _parse_int(value)
    except (TypeError, ValueError, OverflowError):
        result = None
    if result is None or result < low or (high is not None and result > high):
        problems.append(f"{key} is out of range")
        return None
    return result


def parse_fixture(row):
    """Validate one fixture row. A row with problems is kept for display but never acted on."""
    if not isinstance(row, dict):
        return Fixture(name="", enabled=False, problems=("row is not an object",))

    problems = []
    name = row.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        problems.append("name is required")

    try:
        enabled = True if _is_blank(row.get("enabled")) else _parse_bool(row.get("enabled"))
    except ValueError:
        enabled = True
        problems.append("enabled is not true or false")

    model = row.get("model")
    if model is None or (isinstance(model, str) and not model.strip()):
        model = None
    elif not isinstance(model, str) or len(model) > 128 or any(ord(c) < 32 for c in model):
        problems.append("model is not a valid model name")
        model = None
    else:
        model = model.strip()
    universe = _optional_int(row, "universe", 0, None, problems)
    start_channel = _optional_int(row, "start_channel", 1, None, problems)
    dimmer_ch = _optional_int(row, "dimmer_ch", 1, 512, problems)
    shutter_ch = _optional_int(row, "shutter_ch", 1, 512, problems)
    dimmer_off = _optional_int(row, "dimmer_off", 0, 255, problems)
    shutter_off = _optional_int(row, "shutter_off", 0, 255, problems)

    patches = sum(value is not None for value in (model, universe, start_channel))
    if patches > 1:
        problems.append("set one of model, start_channel or universe")
    elif patches == 0 and not problems:
        problems.append("a model or start_channel is required")
    if dimmer_ch is None and shutter_ch is None and not problems:
        problems.append("dimmer_ch or shutter_ch is required")

    return Fixture(
        name=name,
        id=str(row.get("id") or ""),
        enabled=enabled,
        model=model,
        universe=universe,
        start_channel=start_channel,
        dimmer_ch=dimmer_ch,
        dimmer_off=dimmer_off if dimmer_off is not None else 0,
        shutter_ch=shutter_ch,
        shutter_off=shutter_off if shutter_off is not None else 0,
        problems=tuple(problems),
    )


def parse_fixtures(doc):
    if not isinstance(doc, list):
        raise ValueError("fixture list is not an array")
    return tuple(parse_fixture(row) for row in doc)


def _signature(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _read_json(path, default):
    """Return the parsed file, or `default` if it doesn't exist. Other failures raise."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def _read_shared(path):
    """The file's text, or None if a writer holds it. FPP's WriteSettingToFile empties the
    file and rewrites it in place while holding an exclusive flock, so a shared flock here
    means a read never lands between the two."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
        return handle.read()


class ConfigStore:
    """Loads settings, private values and fixtures, and reloads them when the files change."""

    def __init__(self, paths, clock=time.monotonic):
        self.paths = paths
        self._clock = clock
        self._config_keys = set()
        self._suspect = None  # (text, first seen) of a read that looks caught mid-write
        self.settings = Settings()
        self.fixtures = ()
        self._private = {}
        self._signatures = None
        self._load_warnings = ()
        self.reload()

    @property
    def warnings(self):
        return self.settings.warnings + self._load_warnings

    def _read_config(self):
        """The plugin config's values, or None to keep the last good copy and read again."""
        try:
            text = _read_shared(self.paths.config_file)
        except FileNotFoundError:
            return {}
        if text is None:
            return None
        raw = parse_plugin_config(text)
        if self._looks_torn(text, raw):
            return None
        self._config_keys = set(raw)
        self._suspect = None
        return raw

    def _looks_torn(self, text, raw):
        """FPP ends every line and never drops a key, so a read that ends mid-line or has
        lost keys was probably caught mid-write by something that doesn't lock. Read as it
        is, it could turn personal use off and release the heads in a blackout. It's
        believed only once it has stayed the same for a moment."""
        if not ((text and not text.endswith("\n")) or not self._config_keys <= set(raw)):
            return False
        now = self._clock()
        if self._suspect is None or self._suspect[0] != text:
            self._suspect = (text, now)
            return True
        return now - self._suspect[1] < SUSPECT_READ_ACCEPT_S

    def reload(self):
        """Reload if any file changed. Returns True if it did."""
        paths = self.paths
        signatures = tuple(
            _signature(path)
            for path in (paths.config_file, paths.private_file, paths.fixtures_file)
        )
        if signatures == self._signatures:
            return False
        self._signatures = signatures

        problems = []
        try:
            raw = self._read_config()
        except OSError:
            raw = None
            problems.append("Could not read the plugin settings; keeping the last good copy.")
        else:
            if raw is None:
                self._signatures = None  # being written: read it all again next time
                return False
        try:
            private = _read_json(paths.private_file, {})
            if not isinstance(private, dict):
                raise ValueError
            self._private = private
        except (OSError, ValueError):
            problems.append("Could not read private.json; keeping the last good copy.")
        try:
            self.fixtures = parse_fixtures(_read_json(paths.fixtures_file, []))
        except (OSError, ValueError):
            problems.append("Could not read fixtures.json; keeping the last good copy.")

        if raw is not None:
            self.settings = load_settings(raw, self._private)
        else:
            self.settings = replace(
                self.settings,
                online_api_key=str(self._private.get("online_api_key") or ""),
            )
        self._load_warnings = tuple(problems)
        return True
