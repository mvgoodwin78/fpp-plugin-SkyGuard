"""All calls to FPP's HTTP API. Nothing else in the plugin talks to FPP."""

import http.client
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import quote

from . import REPO_NAME
from .notifier import LOGGER_NAME

BASE_URL = "http://localhost"
TIMEOUT_S = 2.0
_MAX_CHANNELS_PER_REQUEST = 100

_logger = logging.getLogger(LOGGER_NAME)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# xLights can send little while a scene holds still, and staying in "show" is the safe side.
BRIDGE_IDLE_S = 30.0


class FppError(Exception):
    """FPP couldn't be reached, or refused the request."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _range_paths(channels):
    """One-channel ranges, grouped so a large show doesn't mean one request per channel."""
    ordered = sorted(channels)
    for start in range(0, len(ordered), _MAX_CHANNELS_PER_REQUEST):
        group = ordered[start : start + _MAX_CHANNELS_PER_REQUEST]
        yield "/api/overlays/range/" + ",".join(f"{channel}-{channel}" for channel in group)


class FppApi:
    def __init__(self, base_url=BASE_URL, timeout_s=TIMEOUT_S):
        self._base_url = base_url
        self._timeout_s = timeout_s

    def _request(self, method, path, body=None, raw_body=None):
        # The JSON header goes only with a JSON body. On FPP 10.3 and 9.5, a PHP route given
        # that header with an empty body answers 404 (seen on a real 10.3 box).
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body.encode("utf-8")  # sent the way FPP's own pages save a setting
        request = urllib.request.Request(self._base_url + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=self._timeout_s) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise FppError(f"HTTP {status}", status) from None
        except (OSError, http.client.HTTPException):
            raise FppError("FPP didn't respond") from None

    def _get_json(self, path):
        try:
            return json.loads(self._request("GET", path))
        except ValueError:
            raise FppError("FPP sent an unreadable response") from None

    def get_status(self):
        return self._get_json("/api/fppd/status")

    def get_bridge_stats(self):
        return self._get_json("/api/fppd/e131stats")

    def get_universe_outputs(self):
        """The E1.31/Art-Net output configuration, or None if there isn't one."""
        try:
            return self._get_json("/api/channel/output/universeOutputs")
        except FppError as error:
            if error.status == 404:
                return None
            raise

    def set_ranges(self, channels, value):
        for path in _range_paths(channels):
            self._request("PUT", path, {"Value": value})

    def delete_ranges(self, channels):
        """Only call for ranges just set. On FPP 9.5-10.2, deleting a missing range means full on."""
        for path in _range_paths(channels):
            self._request("PUT", path, {"delete": True})

    def get_model(self, name):
        return self._get_json(f"/api/overlays/model/{quote(name, safe='')}")

    def set_model_state(self, name, state):
        self._request("PUT", f"/api/overlays/model/{quote(name, safe='')}/state", {"State": state})

    def set_plugin_setting(self, name, value):
        """Saves through FPP, which writes the plugin config as WriteSettingToFile does."""
        path = f"/api/plugin/{REPO_NAME}/settings/{quote(name, safe='')}"
        self._request("PUT", path, raw_body=str(value))

    def get_fonts(self):
        return self._get_json("/api/overlays/fonts")

    def run_command(self, name, args):
        self._request("POST", f"/api/command/{quote(name, safe='')}", [str(arg) for arg in args])


def in_schedule_from_status(status):
    """Whether a show is running. Anything that can't be read counts as a show, failing safe."""
    if not isinstance(status, dict) or not ("status" in status or "status_name" in status):
        return True
    code = status.get("status", 0)
    if code not in (0, "0"):
        return True
    # Live E1.31 or Art-Net input drives the heads with no playlist running.
    if status.get("bridging") not in (None, False, 0, "0"):
        return True
    scheduler = status.get("scheduler")
    if isinstance(scheduler, dict) and scheduler.get("status") in ("playing", "manual"):
        return True
    name = status.get("status_name", "idle")
    return not (isinstance(name, str) and name.strip().lower() == "idle")


def bridge_packet_total(stats):
    """Packets received across all inputs, or None if the stats can't be used."""
    universes = stats.get("universes") if isinstance(stats, dict) else None
    if not isinstance(universes, list):
        return None
    total = 0
    for universe in universes:
        count = universe.get("packetsReceived") if isinstance(universe, dict) else None
        if isinstance(count, int) and not isinstance(count, bool):
            total += count
        elif isinstance(count, str) and count.isdigit():
            total += int(count)
    return total


class ShowDetector:
    """Decides from FPP's status whether a show is running.

    FPP's `bridging` flag is set by the first packet and stays set until FPP restarts, so
    alone it would call an old xLights session a show for days. While it's set, bridged data
    only counts while the packet counters are moving, plus BRIDGE_IDLE_S. Anything that can't
    be read counts as a show, failing safe.
    """

    def __init__(self, api, clock):
        self._api = api
        self._clock = clock
        self._last_total = None
        self._last_moved = None
        self.uptime = None  # fppd's uptime in seconds from the last read, or None

    def read(self):
        self.uptime = None
        try:
            status = self._api.get_status()
        except FppError:
            return True
        if not isinstance(status, dict):
            return True
        uptime = status.get("uptimeTotalSeconds")
        if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
            self.uptime = uptime
        bridging = status.get("bridging") not in (None, False, 0, "0")
        if in_schedule_from_status({**status, "bridging": False}):
            return True
        if not bridging:
            self._last_total = None
            return False
        return self._bridge_data_arriving()

    def _bridge_data_arriving(self):
        try:
            total = bridge_packet_total(self._api.get_bridge_stats())
        except FppError:
            return True
        if total is None:
            return True
        now = self._clock()
        if total != self._last_total:
            self._last_total = total
            self._last_moved = now
        return now - self._last_moved < BRIDGE_IDLE_S
