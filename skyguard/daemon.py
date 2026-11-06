"""The long-running daemon. The trigger engine decides; everything else follows its state.

Order inside each step matters: the channel ranges are applied before anything slower,
and the matrix runs on its own thread, so nothing can delay a blackout.

Run with: python3 -m skyguard.daemon
"""

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from dataclasses import replace

from . import commands
from .config import ConfigStore, Paths
from .feed_manager import FeedManager
from .fpp_api import BASE_URL, FppApi, FppError, read_in_schedule
from .matrix import MatrixController
from .notifier import LOGGER_NAME, LogSink, Notifier, WarningSink
from .overlay import ChannelResolver, Ledger, OverlayController, blackout_target
from .status import StatusWriter, build_status
from .trigger import BLACKOUT, IDLE, Inputs, TriggerEngine, evaluate_zone

ONLINE_LAG_NOTICE = (
    "Online flight data can lag behind real aircraft. Increase your radius by 3 miles to allow for it."
)
STEP_TICK_S = 0.25
SCHEDULE_READ_S = 1.0
SETTING_RETRY_S = 5.0
MATRIX_TICK_S = 0.25
MATRIX_JOIN_S = 0.5

_logger = logging.getLogger(LOGGER_NAME)


def setup_logging(paths):
    os.makedirs(os.path.dirname(paths.log_file), exist_ok=True)
    # WatchedFileHandler reopens the log after FPP rotates it by renaming.
    handler = logging.handlers.WatchedFileHandler(paths.log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class Daemon:
    def __init__(
        self,
        paths,
        api,
        fetch=None,
        clock=time.monotonic,
        wall_clock=time.time,
        supervisor_pid=None,
        parent_pid=os.getppid,
    ):
        self._supervisor_pid = supervisor_pid  # None when not started by the supervisor
        self._parent_pid = parent_pid
        self.paths = paths
        self.api = api
        self._clock = clock
        self._wall_clock = wall_clock
        self._stop = threading.Event()

        self.store = ConfigStore(paths)
        self.warning_sink = WarningSink()
        self.notifier = Notifier([LogSink(), self.warning_sink])

        self.settings = self.store.settings
        feed_args = {} if fetch is None else {"fetch": fetch}
        self.feed = FeedManager(self.settings, self.notifier, clock=clock, **feed_args)

        self.ledger = Ledger(paths.ledger_file)
        resume = self.ledger.resume_blackout
        if resume:
            _logger.warning("Resuming in blackout: an earlier run left channels held.")
        self.engine = TriggerEngine(self.settings.release_delay_s, resume_blackout=resume)
        self._needs_ledger_adopt = self.ledger.corrupt

        self.overlay = OverlayController(api, self.ledger, self.notifier, clock)
        self.resolver = ChannelResolver(api, self.notifier, clock)
        self.matrix = MatrixController(api, self.ledger, clock)
        self.status_writer = StatusWriter(paths.status_file, clock)

        self._in_schedule = False
        self._last_schedule_read = None
        self._pending_enabled = None
        self._last_setting_attempt = None
        self._logged_warnings = set()
        self._lag_notice_logged = False
        self._ground_logged = set()
        self._no_position_logged = set()
        self._skipped_logged = 0
        self._error_active = False
        self._head_test = None  # (fixture id, end time) while one head is being tested

        self._matrix_lock = threading.Lock()
        self._matrix_request = None
        self._matrix_thread = None

    # Settings and commands ------------------------------------------------------

    def _reload_settings(self):
        self.store.reload()
        settings = self.store.settings
        if self._pending_enabled is not None:
            if settings.enabled == self._pending_enabled:
                self._pending_enabled = None
            else:
                self._save_enabled()
                settings = replace(settings, enabled=self._pending_enabled)
        if settings != self.settings:
            self.settings = settings
            self.feed.update_settings(settings)
        self.engine.release_delay_s = settings.release_delay_s

        self.notifier.emit("plugin_enabled" if settings.enabled else "plugin_disabled")
        self.notifier.emit("center_set" if settings.center_set else "center_unset")
        self.notifier.emit("radius_set" if settings.radius_ready else "radius_unset")
        needs_confirmation = settings.enabled and not settings.personal_use_confirmed
        self.notifier.emit("personal_use_required" if needs_confirmation else "personal_use_confirmed")
        if settings.uses_online and not self._lag_notice_logged:
            _logger.info("%s", ONLINE_LAG_NOTICE)
        self._lag_notice_logged = settings.uses_online
        for warning in self.store.warnings:
            if warning not in self._logged_warnings:
                _logger.warning("%s", warning)
        self._logged_warnings = set(self.store.warnings)

    def _save_enabled(self):
        """Persist Enable or Disable through FPP. It already applies in memory if this fails."""
        now = self._clock()
        if self._last_setting_attempt is not None and now - self._last_setting_attempt < SETTING_RETRY_S:
            return
        self._last_setting_attempt = now
        try:
            self.api.set_plugin_setting("enabled", "1" if self._pending_enabled else "0")
            self._last_setting_attempt = None
        except FppError as error:
            _logger.warning("Could not save the enabled setting: %s", error)

    def _check_supervisor(self):
        """If the supervisor dies, this process gets a new parent. Nothing would then restart
        us after a crash, and the heads would be unprotected if that happened in CLEAR."""
        if self._supervisor_pid is not None and self._parent_pid() != self._supervisor_pid:
            self.notifier.emit("supervisor_lost")

    def _run_commands(self):
        for command in commands.read_pending(self.paths, self._wall_clock):
            name = command["command"]
            if name == commands.TEST:
                seconds = commands.test_seconds(command.get("seconds"))
                allowed = self.settings.enabled and self.settings.personal_use_confirmed
                fixture = command.get("fixture")
                if not allowed:
                    _logger.info("Test trigger ignored because SkyGuard is disabled or not confirmed")
                elif fixture:
                    _logger.info("Test of one head for %s s", seconds)
                    self._head_test = (str(fixture), self._clock() + seconds)
                else:
                    _logger.info("Test trigger for %s s", seconds)
                    self.engine.start_test(self._clock(), seconds)
            else:
                self._pending_enabled = name == commands.ENABLE
                _logger.info("SkyGuard %s command received", name)
                self._last_setting_attempt = None

    # One pass of the main loop --------------------------------------------------

    def _read_schedule(self, now):
        due = self._last_schedule_read is None or now - self._last_schedule_read >= SCHEDULE_READ_S
        if due:
            self._last_schedule_read = now
            self._in_schedule = read_in_schedule(self.api)
            self.feed.set_in_schedule(self._in_schedule)

    def _evaluate(self, feed_state):
        settings = self.settings
        if not settings.center_set or not settings.radius_ready:
            return None
        return evaluate_zone(
            feed_state.aircraft,
            settings.center_lat,
            settings.center_lon,
            settings.radius_nm_for(feed_state.active_feed),
            settings.alt_floor_ft,
            settings.alt_ceiling_ft,
            settings.count_no_position,
            ignore_ground=settings.ignore_ground,
            ground_speed_limit_kt=settings.ground_speed_limit_kt,
        )

    def _log_transition(self, transition):
        aircraft = [record.callsign or record.hex for record in transition.aircraft]
        details = {
            "from": transition.from_state,
            "to": transition.to_state,
            "reason": transition.reason,
            "aircraft": aircraft,
        }
        if transition.to_state == BLACKOUT and transition.from_state != BLACKOUT:
            self.notifier.emit("blackout_started", details)
        elif transition.from_state == BLACKOUT and transition.to_state != BLACKOUT:
            self.notifier.emit("blackout_cleared", details)
        else:
            _logger.info("state %s reason %s aircraft %s", transition.to_state, transition.reason, aircraft)

    def _log_skips(self, zone, feed_state):
        ground = {record.hex for record in zone.ground_skipped} if zone else set()
        for hex_code in sorted(ground - self._ground_logged):
            self.notifier.emit("ground_aircraft_ignored", {"aircraft": hex_code})
        self._ground_logged = ground

        no_position = {record.hex for record in zone.no_position_skipped} if zone else set()
        for hex_code in sorted(no_position - self._no_position_logged):
            _logger.info("Skipped aircraft %s: it has no position and count_no_position is off", hex_code)
        self._no_position_logged = no_position

        if feed_state.skipped != self._skipped_logged:
            if feed_state.skipped:
                _logger.info("Skipped %s feed entries with no usable hex", feed_state.skipped)
            self._skipped_logged = feed_state.skipped

    def _hold_after_corrupt_ledger(self, target):
        if self._needs_ledger_adopt and self.resolver.ready:
            self.overlay.adopt_after_corrupt_ledger(target)
            self._needs_ledger_adopt = False

    def step(self):
        """One pass: read inputs, decide, apply. Never raises."""
        try:
            self._step()
            if self._error_active:
                self._error_active = False
                self.notifier.emit("internal_ok")
        except Exception:
            self._handle_step_error()

    def _step(self):
        now = self._clock()
        self._check_supervisor()
        self._run_commands()
        self._reload_settings()
        self._read_schedule(now)

        settings = self.settings
        feed_state = self.feed.state()
        zone = self._evaluate(feed_state)
        in_zone = tuple(a.record for a in zone.in_zone) if zone else ()
        transition = self.engine.update(
            now,
            Inputs(
                enabled=settings.enabled,
                center_set=settings.center_set,
                radius_set=settings.radius_ready,
                personal_use_confirmed=settings.personal_use_confirmed,
                in_schedule=self._in_schedule,
                data_fresh=self.feed.data_fresh(now),
                aircraft=in_zone,
            ),
        )
        if transition is not None:
            self._log_transition(transition)

        resolved = self.resolver.refresh(self.store.fixtures)
        target = blackout_target(resolved)
        self._hold_after_corrupt_ledger(target)
        self._report_fixture_setup(settings, target)
        self.overlay.sync(self._channels_to_hold(now, resolved, target))

        self._request_matrix(zone, in_zone)
        self._log_skips(zone, feed_state)
        self._write_status(now, feed_state, zone, resolved)

    def _report_fixture_setup(self, settings, target):
        missing = settings.enabled and settings.personal_use_confirmed and not target
        self.notifier.emit("no_fixtures" if missing else "fixtures_set")

    def _channels_to_hold(self, now, resolved, target):
        """Everything during a blackout, otherwise only the head being tested, if any."""
        hold = dict(target) if self.engine.state == BLACKOUT else {}
        if self._head_test is not None:
            fixture_id, until = self._head_test
            if now >= until or not (self.settings.enabled and self.settings.personal_use_confirmed):
                self._head_test = None
            else:
                for fixture in resolved:
                    if fixture.id == fixture_id:
                        hold.update({channel: value for channel, value, _role in fixture.channels})
        return hold

    def _handle_step_error(self):
        """Something unexpected happened, so what the heads are doing is unknown. During a
        show, fail dark: a darkened show is better than a beam on an aircraft."""
        _logger.exception("Unexpected error in the main loop")
        if not self._error_active:
            self._error_active = True
            self.notifier.emit("internal_error")
        if self._in_schedule and self.settings.enabled:
            try:
                self.overlay.sync(blackout_target(self.resolver.resolved))
            except Exception:
                _logger.exception("Could not hold the heads dark after the error")

    def _write_status(self, now, feed_state, zone, resolved):
        data_age = None
        if feed_state.last_success_at is not None:
            data_age = now - feed_state.last_success_at
        warnings = self.warning_sink.warnings() + list(self.store.warnings)
        self.status_writer.write(
            build_status(
                self.engine.state,
                self.engine.reason,
                self.settings,
                self._in_schedule,
                feed_state,
                data_age,
                zone,
                resolved,
                warnings,
                self._wall_clock(),
                notices=(ONLINE_LAG_NOTICE,) if self.settings.uses_online else (),
            )
        )

    # The matrix, on its own thread ----------------------------------------------

    def _request_matrix(self, zone, in_zone):
        closest = zone.closest if zone else None
        with self._matrix_lock:
            self._matrix_request = (
                self.engine.state,
                self.engine.reason,
                closest,
                len(in_zone),
                self.settings,
            )

    def matrix_step(self):
        with self._matrix_lock:
            request = self._matrix_request
        if request is not None:
            self.matrix.update(*request)

    def _matrix_loop(self):
        while not self._stop.wait(MATRIX_TICK_S):
            try:
                self.matrix_step()
            except Exception:
                _logger.exception("Unexpected error updating the matrix message")

    # Life cycle -----------------------------------------------------------------

    def request_stop(self, *_args):
        self._stop.set()

    def run(self):
        _logger.info("SkyGuard daemon started")
        self.feed.start()
        self._matrix_thread = threading.Thread(target=self._matrix_loop, name="skyguard-matrix", daemon=True)
        self._matrix_thread.start()
        self.step()
        while not self._stop.wait(STEP_TICK_S):
            self.step()
        self.shutdown()

    def shutdown(self):
        """A clean stop releases everything. A crash doesn't get here, and the heads stay dark."""
        self._stop.set()
        # First, because the stop has a short time budget and a feed may be mid-request.
        released = self.overlay.release_all()
        self.feed.stop(timeout_s=0.2)
        if self._matrix_thread is not None:
            self._matrix_thread.join(MATRIX_JOIN_S)
        try:
            self.matrix.clear_all()
        except Exception:
            _logger.exception("Could not clear the matrix message")
        _logger.info("SkyGuard daemon stopped; channels released: %s", released)


def main(argv=None):
    parser = argparse.ArgumentParser(description="SkyGuard daemon")
    parser.add_argument("--fpp-url", default=BASE_URL, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--supervisor-pid", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    paths = Paths.from_env()
    setup_logging(paths)
    try:
        os.makedirs(paths.data_dir, exist_ok=True)
        with open(paths.daemon_pid_file, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        daemon = Daemon(paths, FppApi(args.fpp_url), supervisor_pid=args.supervisor_pid)
        signal.signal(signal.SIGTERM, daemon.request_stop)
        signal.signal(signal.SIGINT, daemon.request_stop)
        daemon.run()
    except Exception:
        # Exit without releasing: the heads stay as they are, and the supervisor restarts us.
        _logger.exception("The daemon stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
