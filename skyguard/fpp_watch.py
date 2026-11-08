"""Reads FPP's status and output configuration on a thread of its own.

These reads can each take up to FPP's 2 s timeout when FPP is busy. On the main loop they
would hold up a blackout, so the main loop only takes the latest results here, without
waiting. A result too old to trust counts as a show, failing safe.

It also notices fppd restarting, from its uptime going down. A restart drops every
overlay range, and the output configuration may have changed with it, so the daemon
re-sends what it holds at once and the configuration is read again.
"""

import logging
import threading

from .notifier import LOGGER_NAME, RepeatLimiter

STATUS_READ_S = 1.0
STALE_SHOW_READ_S = 3.0  # a show reading older than this isn't trusted: counted as a show

_logger = logging.getLogger(LOGGER_NAME)


class FppWatcher:
    def __init__(self, show_detector, resolver, clock):
        self._show = show_detector
        self._resolver = resolver
        self._clock = clock
        self._lock = threading.Lock()
        self._in_show = True
        self._read_at = None
        self._uptime = None
        self._restarted = False
        self._stop = threading.Event()
        self._thread = None
        self._errors = RepeatLimiter(_logger)

    def poll_once(self):
        """Read FPP's status, and its output configuration if due or after a restart."""
        in_show = self._show.read()
        uptime = self._show.uptime
        now = self._clock()
        restarted = False
        with self._lock:
            self._in_show = in_show
            self._read_at = now
            if uptime is not None:
                restarted = self._uptime is not None and uptime < self._uptime
                self._uptime = uptime
            if restarted:
                self._restarted = True
        if restarted:
            _logger.info("fppd restarted; sending the held channels again")
        self._resolver.read_outputs(force=restarted)

    def in_show(self, now):
        with self._lock:
            if self._read_at is None or now - self._read_at > STALE_SHOW_READ_S:
                return True
            return self._in_show

    def take_restart(self):
        """True once after each fppd restart seen."""
        with self._lock:
            restarted, self._restarted = self._restarted, False
            return restarted

    def _loop(self):
        while not self._stop.wait(STATUS_READ_S):
            try:
                self.poll_once()
                self._errors.clear()
            except Exception:
                self._errors.exception("Unexpected error reading FPP's status")

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="skyguard-fpp-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout_s=0.2):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None
