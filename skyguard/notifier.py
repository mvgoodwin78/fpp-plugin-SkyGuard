"""One internal path for every event, so later sinks (SMS, push, MQTT) change nothing else.

Warning events are de-duplicated here: one all_feeds_down per outage, not one per poll.
"""

import json
import logging
import re
import threading
from dataclasses import dataclass, field

LOGGER_NAME = "skyguard"

_FIXTURE_INVALID = "fixture_invalid"

# Warning key -> the message shown in status data and the header indicator.
_WARNING_MESSAGES = {
    "receiver_lost": "The receiver isn't responding.",
    "receiver_978_lost": "The 978 MHz decoder isn't responding, so its aircraft aren't being seen.",
    "fallback_active": "Using the online feed because the receiver is down.",
    "all_feeds_down": "No aircraft data from any feed.",
    "plugin_disabled": "SkyGuard is disabled, so the heads are not protected.",
    "overlay_failed": "FPP didn't accept a channel change, so the heads may not black out or release.",
    "supervisor_lost": "SkyGuard's supervisor stopped, so nothing will restart it if it crashes. Restart FPPD to fix this.",
    "internal_error": "SkyGuard hit an unexpected error. During a show, the heads are held dark. See the plugin log.",
    "center_unset": "Set a center point. SkyGuard stays idle until you do.",
    "radius_unset": "Enter a radius for each data source in use. SkyGuard stays idle until you do.",
    "personal_use_required": "SkyGuard is for personal use only. It stays idle until you confirm this in its setup.",
}

# Event -> warning key it raises.
_RAISES = {
    "receiver_lost": "receiver_lost",
    "receiver_978_lost": "receiver_978_lost",
    "fallback_active": "fallback_active",
    "all_feeds_down": "all_feeds_down",
    "plugin_disabled": "plugin_disabled",
    "center_unset": "center_unset",
    "radius_unset": "radius_unset",
    "personal_use_required": "personal_use_required",
    "overlay_failed": "overlay_failed",
    "supervisor_lost": "supervisor_lost",
    "internal_error": "internal_error",
}

# Event -> warning keys it clears.
_CLEARS = {
    "receiver_restored": ("receiver_lost", "fallback_active"),
    "receiver_978_restored": ("receiver_978_lost",),
    "fallback_ended": ("fallback_active",),
    "feeds_restored": ("all_feeds_down",),
    "plugin_enabled": ("plugin_disabled",),
    "center_set": ("center_unset",),
    "radius_set": ("radius_unset",),
    "personal_use_confirmed": ("personal_use_required",),
    "overlay_restored": ("overlay_failed",),
    "internal_ok": ("internal_error",),
}

_PLAIN_EVENTS = ("blackout_started", "blackout_cleared", "ground_aircraft_ignored")
_FIXTURE_EVENTS = (_FIXTURE_INVALID, "fixture_resolved")

KNOWN_EVENTS = frozenset((*_RAISES, *_CLEARS, *_PLAIN_EVENTS, *_FIXTURE_EVENTS))

_SENSITIVE_KEY = re.compile(r"key|token|secret|pass|credential|center|lat$|lon$", re.IGNORECASE)


@dataclass(frozen=True)
class Event:
    name: str
    details: dict = field(default_factory=dict)
    raises: str | None = None  # warning key this event raises
    message: str | None = None  # text for that warning
    clears: tuple = ()  # warning keys this event clears


def _fixture_key(details):
    return f"{_FIXTURE_INVALID}:{details.get('fixture', '')}"


class Notifier:
    def __init__(self, sinks=()):
        self.sinks = list(sinks)
        self._active = set()
        self._lock = threading.RLock()  # the feed threads and the main loop both emit
        self._logger = logging.getLogger(LOGGER_NAME)

    def emit(self, event_name, details=None):
        """Send an event to every sink. Returns False if it was suppressed as a repeat."""
        if event_name not in KNOWN_EVENTS:
            raise ValueError(f"unknown event: {event_name}")
        details = dict(details or {})
        event = self._build(event_name, details)

        with self._lock:
            if event.raises is not None:
                if event.raises in self._active:
                    return False
                self._active.add(event.raises)
            elif event_name not in _PLAIN_EVENTS:
                cleared = [key for key in event.clears if key in self._active]
                if not cleared:
                    return False
                self._active.difference_update(cleared)

            for sink in self.sinks:
                try:
                    sink.handle(event)
                except Exception:
                    # A failing sink must never stop the trigger engine or the other sinks.
                    self._logger.exception("Notification sink %s failed", type(sink).__name__)
        return True

    @staticmethod
    def _build(name, details):
        if name == _FIXTURE_INVALID:
            fixture = details.get("fixture", "")
            return Event(
                name,
                details,
                raises=_fixture_key(details),
                message=f"Fixture '{fixture}' can't be resolved, so it won't black out.",
            )
        if name == "fixture_resolved":
            return Event(name, details, clears=(_fixture_key(details),))
        if name in _RAISES:
            key = _RAISES[name]
            return Event(name, details, raises=key, message=_WARNING_MESSAGES[key])
        return Event(name, details, clears=_CLEARS.get(name, ()))


class LogSink:
    """Writes each event to the plugin log, with sensitive details removed."""

    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger(LOGGER_NAME)

    def handle(self, event):
        level = logging.WARNING if event.raises else logging.INFO
        safe = {
            key: ("[redacted]" if _SENSITIVE_KEY.search(key) else value)
            for key, value in event.details.items()
        }
        # JSON escapes newlines, so a callsign or fixture name can't forge a log line.
        self._logger.log(level, "%s %s", event.name, json.dumps(safe, default=str, sort_keys=True))


class WarningSink:
    """Keeps the active warnings for the status file and the header indicator."""

    def __init__(self):
        self._warnings = {}
        self._lock = threading.Lock()

    def handle(self, event):
        with self._lock:
            if event.raises:
                self._warnings[event.raises] = event.message
            for key in event.clears:
                self._warnings.pop(key, None)

    def warnings(self):
        with self._lock:
            return list(self._warnings.values())
