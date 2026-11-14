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
from .geo import to_nm

DEFAULT_MEDIA_DIR = "/home/fpp/media"
FEED_RECEIVER = "receiver"
FEED_ONLINE = "online"
SOURCE_MODES = ("receiver", "online", "both")
RECEIVER_LOCATIONS = ("other", "this_box")
RADIUS_UNITS = ("mi", "nm", "km")
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
            commands_dir=os.path.join(data, "commands"),
            supervisor_pid_file=os.path.join(data, "supervisor.pid"),
            supervisor_lock_file=os.path.join(data, "supervisor.lock"),
            daemon_pid_file=os.path.join(data, "daemon.pid"),
            log_file=os.path.join(logs, f"plugin-{REPO_NAME}.log"),
        )


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
    radius: float | None = None  # used with online data; the user must enter it
    radius_receiver: float | None = None  # used with a receiver; the user must enter it
    radius_unit: str = "mi"
    alt_floor_ft: float = 0.0
    alt_ceiling_ft: float = 10000.0
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

    @property
    def radius_ready(self):
        """Whether a radius has been entered for every data source in use."""
        if self.uses_online and self.radius is None:
            return False
        return not (self.uses_receiver and self.radius_receiver is None)

    def radius_for(self, feed):
        """The radius entered for `feed`, in the user's unit, or None."""
        return self.radius_receiver if feed == FEED_RECEIVER else self.radius

    def radius_nm_for(self, feed):
        value = self.radius_for(feed)
        return None if value is None else to_nm(value, self.radius_unit)

    @property
    def radius_nm(self):
        """The radius used with online data, which is also what online queries must cover."""
        return self.radius_nm_for(FEED_ONLINE)


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
        radius=read.number("radius", 0.01),
        radius_receiver=read.number("radius_receiver", 0.01),
        radius_unit=read.choice("radius_unit", RADIUS_UNITS),
        alt_floor_ft=read.number("alt_floor_ft"),
        alt_ceiling_ft=read.number("alt_ceiling_ft"),
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
    if settings.alt_floor_ft > settings.alt_ceiling_ft:
        defaults = Settings()
        warnings.append("The altitude floor is above the ceiling; using the default altitude band.")
        settings = replace(
            settings,
            alt_floor_ft=defaults.alt_floor_ft,
            alt_ceiling_ft=defaults.alt_ceiling_ft,
        )
    if settings.radius_nm is not None and settings.radius_nm > MAX_QUERY_RADIUS_NM:
        warnings.append(
            f"The radius is larger than the {MAX_QUERY_RADIUS_NM} nm online feeds can query, "
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
