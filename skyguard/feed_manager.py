"""Polling, receiver health, fallback and data age. Exactly one feed is active at a time.

The poll, health and 978 loops run on separate threads so a dead host, which
takes a full timeout to fail, can't delay the others. No lock is held during a
request. A result that arrives after the active feed or settings changed is
discarded rather than applied to the wrong state.

A poll only counts as successful if the feed's `now` has moved. A decoder can die
while its web server keeps serving the last aircraft.json, and that frozen file
must not look like a healthy, possibly empty, sky.
"""

import logging
import threading
import time
from dataclasses import dataclass

from .feeds.base import FeedError
from .feeds.http_client import FeedHttpError, fetch_json
from .feeds.online_adsbx_v2 import PROVIDERS, OnlineAdapter
from .feeds.receiver import (
    SOURCE_RECEIVER,
    SOURCE_RECEIVER_978,
    ReceiverAdapter,
    merge_snapshots,
)
from .notifier import LOGGER_NAME
from .trigger import data_is_fresh

FEED_RECEIVER = "receiver"
FEED_ONLINE = "online"

FROZEN_GRACE_S = 3.0  # decoders write about once a second; beyond this, `now` is stuck
MAX_ONLINE_BACKOFF_S = 30.0
MIN_978_DATA_AGE_S = 5.0
DISPLAY_RANGE_RADII = 2.0
LOOP_TICK_S = 0.1

_logger = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class FeedState:
    active_feed: str
    polling: bool  # False when the active feed isn't allowed to send requests
    receiver_ok: bool | None  # None when no receiver is in use
    aircraft: tuple
    skipped: int
    last_success_at: float | None
    last_poll_failed: bool


class FeedManager:
    def __init__(self, settings, notifier, fetch=fetch_json, clock=time.monotonic):
        self._lock = threading.RLock()
        self._notifier = notifier
        self._fetch = fetch
        self._clock = clock
        self._settings = settings
        self._generation = 0

        self._in_schedule = False
        self._force_poll = False
        self._last_attempt = None
        self._online_4xx_count = 0

        self._aircraft = ()
        self._skipped = 0
        self._last_success_at = None
        self._last_poll_failed = False  # carries across a switch, so old data never looks fresh
        self._current_feed_failing = False  # resets on a switch: a new feed is innocent until tried
        self._last_feed_now = None
        self._last_advance = None
        self._poll_error = None

        self._reset_receiver_tracking()
        self._active = self._choose_active()

        self._stop = threading.Event()
        self._threads = []

    # Configuration helpers ------------------------------------------------------

    def _reset_receiver_tracking(self):
        self._receiver_healthy = True
        self._health_failures = 0
        self._last_health = None
        self._health_last_now = None
        self._second_snapshot = None
        self._second_at = None
        self._second_last_attempt = None
        self._second_last_now = None
        self._second_last_advance = None
        self._second_failing_since = None

    @property
    def _uses_receiver(self):
        return self._settings.source_mode in ("receiver", "both")

    @property
    def _primary_url(self):
        settings = self._settings
        if not self._uses_receiver:
            return ""
        return settings.receiver_url or settings.receiver_978_url

    @property
    def _second_url(self):
        settings = self._settings
        if not self._uses_receiver or not settings.receiver_url:
            return ""
        return settings.receiver_978_url

    @property
    def _online_allowed(self):
        return self._settings.enabled and self._settings.center_set

    def _choose_active(self):
        mode = self._settings.source_mode
        if mode == "receiver":
            return FEED_RECEIVER
        if mode == "online" or not self._primary_url:
            return FEED_ONLINE
        if self._receiver_healthy or not self._online_allowed:
            return FEED_RECEIVER
        return FEED_ONLINE

    @property
    def _can_poll(self):
        return self._active == FEED_RECEIVER or self._online_allowed

    def _primary_adapter(self):
        if not self._primary_url:
            return None
        source = SOURCE_RECEIVER if self._settings.receiver_url else SOURCE_RECEIVER_978
        return ReceiverAdapter(self._primary_url, source, self._fetch)

    def _online_adapter(self):
        settings = self._settings
        return OnlineAdapter(
            settings.online_provider,
            settings.center_lat,
            settings.center_lon,
            settings.radius_nm * DISPLAY_RANGE_RADII,
            self._fetch,
        )

    def _poll_interval(self):
        settings = self._settings
        if self._active == FEED_RECEIVER:
            return settings.poll_interval_s
        minimum = PROVIDERS[settings.online_provider].min_interval_s
        wanted = settings.poll_interval_s if self._in_schedule else settings.online_idle_interval_s
        interval = max(wanted, minimum)
        if self._online_4xx_count:
            # Repeated refusals can get an address restricted, so back off while refused.
            backoff = min(interval * 2 ** self._online_4xx_count, MAX_ONLINE_BACKOFF_S)
            interval = max(interval, backoff)
        return interval

    # Changes from the daemon ----------------------------------------------------

    def set_in_schedule(self, in_schedule):
        """Tell the manager about the show window. A new window polls at once."""
        with self._lock:
            if in_schedule and not self._in_schedule:
                self._force_poll = True
            self._in_schedule = in_schedule

    def update_settings(self, settings):
        with self._lock:
            old = self._settings
            self._settings = settings
            receiver_changed = (old.source_mode, old.receiver_url, old.receiver_978_url) != (
                settings.source_mode,
                settings.receiver_url,
                settings.receiver_978_url,
            )
            query_changed = (
                old.online_provider,
                old.center_lat,
                old.center_lon,
                old.radius_nm,
            ) != (
                settings.online_provider,
                settings.center_lat,
                settings.center_lon,
                settings.radius_nm,
            )
            if receiver_changed:
                self._reset_receiver_tracking()
                self._notifier.emit("receiver_restored")
                self._notifier.emit("receiver_978_restored")
            if old.enabled != settings.enabled or old.center_set != settings.center_set:
                self._generation += 1  # a request in flight may no longer be wanted
            if receiver_changed or query_changed:
                self._generation += 1
                self._force_poll = True
                self._online_4xx_count = 0
                self._last_feed_now = None
            self._refresh_active()
            self._update_feeds_down(self._clock())

    def _refresh_active(self):
        new_active = self._choose_active()
        if new_active == self._active:
            return
        self._active = new_active
        self._generation += 1
        self._force_poll = True
        self._last_feed_now = None
        self._current_feed_failing = False
        self._poll_error = None
        if not self._primary_url:
            return
        if new_active == FEED_ONLINE:
            self._notifier.emit("fallback_active", {"provider": self._settings.online_provider})
        else:
            self._notifier.emit("fallback_ended")

    # Polling the active feed ----------------------------------------------------

    def _poll_is_due(self, now):
        if not self._can_poll:
            return False
        if self._force_poll or self._last_attempt is None:
            return True
        return now - self._last_attempt >= self._poll_interval()

    def poll_once(self):
        """Poll the active feed if it is due. Returns True if a poll was attempted."""
        with self._lock:
            if not self._poll_is_due(self._clock()):
                return False
            self._last_attempt = self._clock()
            self._force_poll = False
            generation = self._generation
            on_receiver = self._active == FEED_RECEIVER
            adapter = self._primary_adapter() if on_receiver else self._online_adapter()

        snapshot = error = None
        try:
            if adapter is None:
                raise FeedError("no receiver URL is set")
            snapshot = adapter.fetch()
        except FeedError as failure:
            error = failure

        with self._lock:
            if generation != self._generation:
                return True
            now = self._clock()
            if error is not None:
                self._poll_failed(error)
            else:
                self._poll_answered(now, snapshot)
            self._update_feeds_down(now)
        return True

    def _poll_answered(self, now, snapshot):
        if snapshot.now_s == self._last_feed_now:
            if now - self._last_advance > FROZEN_GRACE_S:
                self._poll_failed(FeedError("data is not updating"))
            return
        self._last_feed_now = snapshot.now_s
        self._last_advance = now
        self._last_success_at = now
        self._last_poll_failed = False
        self._current_feed_failing = False
        self._online_4xx_count = 0
        if self._poll_error is not None:
            _logger.info("%s feed is delivering data again", self._active)
            self._poll_error = None
        if self._active == FEED_RECEIVER:
            snapshot = self._with_second_feed(now, snapshot)
        self._aircraft = snapshot.aircraft
        self._skipped = snapshot.skipped

    def _poll_failed(self, error):
        self._last_poll_failed = True
        self._current_feed_failing = True
        if self._active == FEED_ONLINE and isinstance(error, FeedHttpError) and 400 <= error.status < 500:
            self._online_4xx_count += 1
        message = str(error)
        if message != self._poll_error:
            _logger.warning("%s feed poll failed: %s", self._active, message)
            self._poll_error = message

    def _with_second_feed(self, now, snapshot):
        if self._second_snapshot is None:
            return snapshot
        limit = max(self._settings.max_data_age_s, MIN_978_DATA_AGE_S)
        if now - self._second_at > limit:
            return snapshot
        return merge_snapshots(snapshot, self._second_snapshot)

    def _update_feeds_down(self, now):
        waiting_on_health = (
            self._active == FEED_RECEIVER
            and self._settings.source_mode == "both"
            and self._receiver_healthy
            and self._online_allowed
        )
        if not self._can_poll or waiting_on_health:
            down = False  # online is still available as a fallback, or the feed is off by design
        else:
            down = self._current_feed_failing and not self.data_fresh(now)
        self._notifier.emit("all_feeds_down" if down else "feeds_restored")

    # Receiver health, in Both mode ----------------------------------------------

    def health_once(self):
        """Run the receiver health check if it is due. Returns True if one was attempted."""
        with self._lock:
            settings = self._settings
            if settings.source_mode != "both" or not self._primary_url:
                return False
            now = self._clock()
            if self._last_health is not None and now - self._last_health < settings.health_interval_s:
                return False
            self._last_health = now
            adapter = self._primary_adapter()

        try:
            snapshot = adapter.fetch()
        except FeedError:
            snapshot = None

        with self._lock:
            # Feed switches don't matter here, only whether this is still the receiver in use.
            if self._settings.source_mode == "both" and self._primary_url == adapter.url:
                frozen = snapshot is not None and snapshot.now_s == self._health_last_now
                if snapshot is None or frozen:
                    self._health_failed()
                else:
                    self._health_last_now = snapshot.now_s
                    self._health_passed()
                self._update_feeds_down(self._clock())
        return True

    def _health_failed(self):
        self._health_failures += 1
        if self._receiver_healthy and self._health_failures >= self._settings.health_fail_count:
            self._receiver_healthy = False
            self._notifier.emit("receiver_lost", {"failed_checks": self._health_failures})
            self._refresh_active()

    def _health_passed(self):
        self._health_failures = 0
        if not self._receiver_healthy:
            self._receiver_healthy = True
            self._notifier.emit("receiver_restored")
            self._refresh_active()

    # The optional second (978 MHz) decoder --------------------------------------

    def poll_second_once(self):
        """Poll the second decoder if it is due. Returns True if a poll was attempted."""
        with self._lock:
            url = self._second_url
            if not url or self._active != FEED_RECEIVER:
                return False
            now = self._clock()
            interval = self._settings.poll_interval_s
            if self._second_last_attempt is not None and now - self._second_last_attempt < interval:
                return False
            self._second_last_attempt = now
            generation = self._generation
            adapter = ReceiverAdapter(url, SOURCE_RECEIVER_978, self._fetch)

        try:
            snapshot = adapter.fetch()
        except FeedError:
            snapshot = None

        with self._lock:
            if generation == self._generation:
                self._second_answered(self._clock(), snapshot)
        return True

    def _second_answered(self, now, snapshot):
        if snapshot is not None and snapshot.now_s != self._second_last_now:
            self._second_last_now = snapshot.now_s
            self._second_last_advance = now
            self._second_snapshot = snapshot
            self._second_at = now
            self._second_failing_since = None
            self._notifier.emit("receiver_978_restored")
            return
        stuck = snapshot is not None and now - self._second_last_advance > FROZEN_GRACE_S
        if snapshot is not None and not stuck:
            return
        if self._second_failing_since is None:
            self._second_failing_since = now
        settings = self._settings
        if now - self._second_failing_since >= settings.health_interval_s * settings.health_fail_count:
            self._notifier.emit("receiver_978_lost")

    # What the daemon reads ------------------------------------------------------

    def data_fresh(self, now=None):
        with self._lock:
            now = self._clock() if now is None else now
            return data_is_fresh(
                now,
                self._last_success_at,
                self._last_poll_failed,
                self._settings.max_data_age_s,
            )

    def state(self):
        with self._lock:
            if self._settings.source_mode == "both" and self._primary_url:
                receiver_ok = self._receiver_healthy
            elif self._settings.source_mode == "receiver":
                receiver_ok = self._last_success_at is not None and not self._last_poll_failed
            else:
                receiver_ok = None
            return FeedState(
                active_feed=self._active,
                polling=self._can_poll,
                receiver_ok=receiver_ok,
                aircraft=self._aircraft,
                skipped=self._skipped,
                last_success_at=self._last_success_at,
                last_poll_failed=self._last_poll_failed,
            )

    # Threads --------------------------------------------------------------------

    def _loop(self, step, tick_s):
        while not self._stop.wait(tick_s):
            try:
                step()
            except Exception:
                # A surprise here must not end polling: that would freeze the feed
                # state silently. The data age then fails dark on its own.
                _logger.exception("Feed loop error in %s", step.__name__)

    def start(self, tick_s=LOOP_TICK_S):
        self._stop.clear()
        for step in (self.poll_once, self.health_once, self.poll_second_once):
            thread = threading.Thread(
                target=self._loop,
                args=(step, tick_s),
                name=f"skyguard-{step.__name__}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout_s=0.5):
        """Stop the loops without waiting out a request in progress; the threads are daemons."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout_s)
        self._threads = []
