"""The optional message on the user's LED panels. It can fail without affecting a blackout.

Why it is driven the way it is, from FPP's source:

- Auto-enable isn't used to show text. It makes Center text stop after 25 ms, so the
  state is set first and the text is sent with auto-enable off.
- Clearing relies on Stop Effects to disable a model we enabled. A separate state
  call afterwards would cancel a pending stop on current FPP.
- A scrolling message makes one pass and ends. It is only replaced once that pass is
  finished: restarting it whenever {distance} changed would mean it never finished.
"""

import logging
import re

from .feeds.base import GROUND
from .fpp_api import FppError
from .geo import from_nm
from .notifier import LOGGER_NAME
from .trigger import BLACKOUT, REASON_NO_DATA

REFRESH_INTERVAL_S = 2.0
EFFECT_COMMAND = "Overlay Model Effect"
STATE_DISABLED = 0
STATE_ENABLED = 1
AUTO_OFF = "False"
AUTO_DISABLE_WHEN_STOPPED = "Enabled"
_UNKNOWN = "--"
PREFERRED_FONTS = ("FreeSansBold", "FreeSans", "Helvetica-Bold", "Helvetica")
_PLACEHOLDER = re.compile(r"\{(callsign|altitude|speed|distance|heading|count)\}")

_logger = logging.getLogger(LOGGER_NAME)


def _clean(text):
    """Feed text reaches the panel, so drop control characters."""
    return "".join(c for c in str(text) if c.isprintable())


def _whole(value):
    return _UNKNOWN if value is None else str(round(value))


def fill_message(template, closest, count, unit):
    """Fill placeholders from the closest in-zone aircraft (an EvaluatedAircraft, or None)."""
    values = {key: _UNKNOWN for key in ("callsign", "altitude", "speed", "distance", "heading")}
    values["count"] = str(count)
    if closest is not None:
        record = closest.record
        values["callsign"] = _clean(record.callsign or record.hex)
        values["altitude"] = "ground" if record.alt_ft == GROUND else _whole(record.alt_ft)
        values["speed"] = _whole(record.gs_kt)
        values["heading"] = _whole(record.track_deg)
        if closest.distance_nm is not None:
            values["distance"] = f"{from_nm(closest.distance_nm, unit):.1f}"
    # One pass, so a placeholder inside a callsign from the feed is never expanded.
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


class MatrixController:
    """Call update() on every loop. Models it changed are recorded in the ledger, so a
    restarted daemon can still put them back."""

    def __init__(self, api, ledger, clock):
        self._api = api
        self._ledger = ledger
        self._clock = clock
        self._last_closest = None
        self._font = None
        self._sent = {}  # model -> text last sent by this run
        self._last_attempt = {}  # model -> time of the last request
        self._error_logged = set()

    def update(self, state, reason, closest, count, settings):
        """Show or clear the message to match the current state."""
        if state != BLACKOUT:
            self._last_closest = None
        elif closest is not None:
            self._last_closest = closest

        text = self._message(state, reason, count, settings)
        wanted = settings.matrix_models if text else ()
        for model in wanted:
            self._show(model, text, settings)
        for model in [m for m in self._ledger.matrix if m not in wanted]:
            self._clear(model)

    def clear_all(self):
        for model in list(self._ledger.matrix):
            self._clear(model, force=True)

    def _message(self, state, reason, count, settings):
        if state != BLACKOUT or not settings.matrix_enabled:
            return ""
        if reason == REASON_NO_DATA:
            return settings.matrix_nodata_message
        # During the release delay the zone is empty; keep describing the last aircraft.
        return fill_message(settings.matrix_message, self._last_closest, count, settings.radius_unit)

    def _rate_limited(self, model):
        last = self._last_attempt.get(model)
        return last is not None and self._clock() - last < REFRESH_INTERVAL_S

    def _show(self, model, text, settings):
        if self._rate_limited(model):
            return
        previous = self._sent.get(model)
        scrolling = settings.matrix_position != "Center"
        if previous == text and not scrolling:
            return
        self._last_attempt[model] = self._clock()
        try:
            info = self._api.get_model(model)
            current = int(info.get("isActive", STATE_DISABLED))
            if model not in self._ledger.matrix:
                self._ledger.matrix[model] = current
                self._ledger.save()
            elif scrolling and previous is not None and info.get("effectRunning"):
                return  # let the current pass finish
            if current == STATE_DISABLED:
                # Checked every time, so a failed attempt is retried instead of
                # sending text to a model that can't show it.
                self._api.set_model_state(model, STATE_ENABLED)
            self._api.run_command(EFFECT_COMMAND, self._text_args(model, text, settings))
            self._sent[model] = text
            self._error_logged.discard(model)
        except (FppError, ValueError, TypeError, AttributeError) as error:
            self._log_once(model, f"Could not show the message on '{model}': {error}")

    def _text_args(self, model, text, settings):
        return [
            model,
            AUTO_OFF,
            "Text",
            settings.matrix_color,
            settings.matrix_font or self._first_font(),
            settings.matrix_size,
            "false",
            settings.matrix_position,
            settings.matrix_speed,
            0,
            text,
        ]

    def _first_font(self):
        if self._font is None:
            fonts = self._api.get_fonts()
            if not isinstance(fonts, list) or not fonts:
                raise FppError("FPP lists no fonts")
            # The first listed can be hard to read: on a real 10.3 box it was a bold italic serif.
            preferred = [font for font in PREFERRED_FONTS if font in fonts]
            self._font = str(preferred[0] if preferred else fonts[0])
        return self._font

    def _clear(self, model, force=False):
        if not force and self._rate_limited(model):
            return
        self._last_attempt[model] = self._clock()
        prior = self._ledger.matrix[model]
        auto = AUTO_DISABLE_WHEN_STOPPED if prior == STATE_DISABLED else AUTO_OFF
        try:
            self._api.run_command(EFFECT_COMMAND, [model, auto, "Stop Effects"])
        except FppError as error:
            if not self._model_is_gone(model):
                self._log_once(model, f"Could not clear the message on '{model}': {error}")
                return
            _logger.warning("Message model '%s' is gone; forgetting it.", model)
        del self._ledger.matrix[model]
        self._ledger.save()
        self._sent.pop(model, None)
        self._last_attempt.pop(model, None)
        self._error_logged.discard(model)

    def _model_is_gone(self, model):
        """Only FPP saying so counts. Any other error might leave the message stuck on the panel."""
        try:
            self._api.get_model(model)
            return False
        except FppError as error:
            return error.status == 404

    def _log_once(self, model, message):
        if model not in self._error_logged:
            _logger.warning("%s", message)
            self._error_logged.add(model)
