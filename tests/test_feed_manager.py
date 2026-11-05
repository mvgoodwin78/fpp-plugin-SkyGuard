"""Feed manager logic with a fake clock and fake network, so every timing rule is exact."""

import threading
import unittest
from unittest import mock

from skyguard.config import Settings
from skyguard.feed_manager import (
    FEED_ONLINE,
    FEED_RECEIVER,
    FROZEN_GRACE_S,
    MAX_ONLINE_BACKOFF_S,
    FeedManager,
)
from skyguard.feeds.base import FeedError
from skyguard.feeds.http_client import FeedHttpError
from skyguard.feeds.online_adsbx_v2 import PROVIDERS, Provider
from skyguard.notifier import Notifier, WarningSink

RX = "http://rx/aircraft.json"
RX978 = "http://rx978/aircraft.json"
ONLINE = "https://api.adsb.lol/"
PLANE = {"hex": "a1", "alt_baro": 3000, "lat": 40.0, "lon": -75.0, "seen_pos": 0.5}
PLANE_978 = {"hex": "b2", "alt_baro": 2000, "lat": 40.1, "lon": -75.1, "seen_pos": 0.5}
ONLINE_PLANE = {"hex": "c3", "alt_baro": 1000, "lat": 40.2, "lon": -75.2, "seen_pos": 0.5}


def settings(**overrides):
    values = {
        "source_mode": "both",
        "receiver_url": RX,
        "center_lat": 40.0,
        "center_lon": -75.0,
    }
    values.update(overrides)
    return Settings(**values)


class RecordingSink:
    def __init__(self):
        self.names = []

    def handle(self, event):
        self.names.append(event.name)


class FakeNet:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.frozen = set()
        self.nows = {RX: 1000.0, RX978: 5000.0, ONLINE: 9000.0}
        self.hook = None

    def fetch(self, url):
        self.calls.append(url)
        key = RX if url == RX else RX978 if url == RX978 else ONLINE
        if self.hook is not None:
            self.hook(url)
        if key in self.errors:
            raise self.errors[key]
        if key not in self.frozen:
            self.nows[key] += 1.0
        if key == ONLINE:
            return {"ac": [ONLINE_PLANE], "msg": "No error", "now": self.nows[key] * 1000}
        return {"now": self.nows[key], "aircraft": [PLANE_978 if key == RX978 else PLANE]}

    def count(self, prefix):
        return sum(1 for url in self.calls if url.startswith(prefix))


class Harness:
    def __init__(self, **overrides):
        self.time = 0.0
        self.net = FakeNet()
        self.events = RecordingSink()
        self.warnings = WarningSink()
        self.manager = FeedManager(
            settings(**overrides),
            Notifier([self.events, self.warnings]),
            fetch=self.net.fetch,
            clock=lambda: self.time,
        )

    def step(self):
        self.manager.poll_once()
        self.manager.poll_second_once()
        self.manager.health_once()

    def run_for(self, seconds, tick=1.0):
        end = self.time + seconds
        while self.time < end:
            self.time += tick
            self.step()

    def hexes(self):
        return sorted(record.hex for record in self.manager.state().aircraft)


class ReceiverPollingTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(source_mode="receiver")

    def test_polls_every_second_by_default(self):
        self.h.run_for(10)
        self.assertEqual(self.h.net.count(RX), 10)

    def test_uses_the_interval_setting(self):
        h = Harness(source_mode="receiver", poll_interval_s=2.0)
        h.run_for(10)
        self.assertEqual(h.net.count(RX), 5)

    def test_stays_fast_outside_the_show_window(self):
        self.h.manager.set_in_schedule(False)
        self.h.run_for(10)
        self.assertEqual(self.h.net.count(RX), 10)

    def test_stays_fast_while_disabled(self):
        h = Harness(source_mode="receiver", enabled=False)
        h.run_for(10)
        self.assertEqual(h.net.count(RX), 10)

    def test_stays_fast_with_no_center(self):
        h = Harness(source_mode="receiver", center_lat=None, center_lon=None)
        h.run_for(10)
        self.assertEqual(h.net.count(RX), 10)

    def test_delivers_aircraft(self):
        self.h.run_for(1)
        state = self.h.manager.state()
        self.assertEqual(state.active_feed, FEED_RECEIVER)
        self.assertEqual(self.h.hexes(), ["a1"])
        self.assertEqual(state.last_success_at, 1.0)
        self.assertTrue(self.h.manager.data_fresh())

    def test_never_contacts_online_in_receiver_mode(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(60)
        self.assertEqual(self.h.net.count(ONLINE), 0)

    def test_health_checks_are_for_both_mode_only(self):
        self.assertFalse(self.h.manager.health_once())


class OnlinePollingTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(source_mode="online")

    def test_outside_the_window_polls_every_30_seconds(self):
        self.h.run_for(120)
        self.assertEqual(self.h.net.count(ONLINE), 4)

    def test_idle_interval_is_a_setting(self):
        h = Harness(source_mode="online", online_idle_interval_s=60.0)
        h.run_for(120)
        self.assertEqual(h.net.count(ONLINE), 2)

    def test_inside_the_window_polls_at_the_normal_rate(self):
        self.h.manager.set_in_schedule(True)
        self.h.run_for(10)
        self.assertEqual(self.h.net.count(ONLINE), 10)

    def test_window_start_polls_immediately(self):
        self.h.run_for(1)
        self.assertEqual(self.h.net.count(ONLINE), 1)
        self.h.run_for(5)
        self.assertEqual(self.h.net.count(ONLINE), 1)
        self.h.manager.set_in_schedule(True)
        self.h.manager.poll_once()  # no time has passed
        self.assertEqual(self.h.net.count(ONLINE), 2)

    def test_window_start_on_a_receiver_polls_immediately_too(self):
        h = Harness(source_mode="receiver", poll_interval_s=2.0)
        h.run_for(1)
        h.manager.set_in_schedule(True)
        h.manager.poll_once()
        self.assertEqual(h.net.count(RX), 2)

    def test_staying_in_the_window_forces_nothing_more(self):
        self.h.manager.set_in_schedule(True)
        self.h.manager.poll_once()
        self.h.manager.set_in_schedule(True)
        self.h.manager.poll_once()
        self.assertEqual(self.h.net.count(ONLINE), 1)

    def test_window_end_returns_to_the_slow_rate(self):
        self.h.manager.set_in_schedule(True)
        self.h.run_for(5)
        self.h.manager.set_in_schedule(False)
        before = self.h.net.count(ONLINE)
        self.h.run_for(29)
        self.assertEqual(self.h.net.count(ONLINE), before)
        self.h.run_for(1)
        self.assertEqual(self.h.net.count(ONLINE), before + 1)

    def test_no_requests_while_disabled(self):
        h = Harness(source_mode="online", enabled=False)
        h.manager.set_in_schedule(True)
        h.run_for(120)
        self.assertEqual(h.net.calls, [])
        self.assertFalse(h.manager.state().polling)

    def test_no_requests_with_no_center(self):
        h = Harness(source_mode="online", center_lat=None, center_lon=None)
        h.manager.set_in_schedule(True)
        h.run_for(120)
        self.assertEqual(h.net.calls, [])

    def test_disabling_stops_requests_and_enabling_polls_at_once(self):
        self.h.manager.set_in_schedule(True)
        self.h.run_for(3)
        self.h.manager.update_settings(settings(source_mode="online", enabled=False))
        before = self.h.net.count(ONLINE)
        self.h.run_for(60)
        self.assertEqual(self.h.net.count(ONLINE), before)
        self.h.manager.update_settings(settings(source_mode="online"))
        self.h.manager.poll_once()
        self.assertEqual(self.h.net.count(ONLINE), before + 1)

    def test_provider_minimum_wins_over_a_faster_setting(self):
        slow = {"adsb.lol": Provider("adsb.lol", ONLINE + "{lat}/{lon}/{nm}", 5.0)}
        with mock.patch.dict(PROVIDERS, slow):
            h = Harness(source_mode="online")
            h.manager.set_in_schedule(True)
            h.run_for(20)
        self.assertEqual(h.net.count(ONLINE), 4)

    def test_the_query_uses_the_center_and_twice_the_radius(self):
        h = Harness(source_mode="online", radius=10.0, radius_unit="nm")
        h.run_for(1)
        self.assertEqual(h.net.calls[0], "https://api.adsb.lol/v2/point/40.0000/-75.0000/20")

    def test_online_records_are_tagged_with_the_provider(self):
        self.h.run_for(1)
        self.assertEqual(self.h.manager.state().aircraft[0].source, "online:adsb.lol")


class OnlineRefusalTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(source_mode="online")
        self.h.manager.set_in_schedule(True)

    def gaps(self, seconds):
        self.h.net.calls.clear()
        self.h.run_for(seconds)
        return self.h.net.count(ONLINE)

    def test_a_429_counts_as_a_failed_poll(self):
        self.h.net.errors[ONLINE] = FeedHttpError(429)
        self.h.run_for(1)
        self.assertTrue(self.h.manager.state().last_poll_failed)

    def test_refusals_back_off_and_never_speed_up(self):
        self.h.net.errors[ONLINE] = FeedHttpError(429)
        self.assertLess(self.gaps(30), 7)

    def test_backoff_is_capped(self):
        self.h.net.errors[ONLINE] = FeedHttpError(403)
        self.h.run_for(600)
        self.assertGreaterEqual(self.gaps(int(MAX_ONLINE_BACKOFF_S) * 2), 2)

    def test_success_ends_the_backoff(self):
        self.h.net.errors[ONLINE] = FeedHttpError(429)
        self.h.run_for(60)
        del self.h.net.errors[ONLINE]
        self.h.run_for(int(MAX_ONLINE_BACKOFF_S))
        self.assertEqual(self.gaps(10), 10)

    def test_server_errors_and_timeouts_do_not_back_off(self):
        self.h.net.errors[ONLINE] = FeedHttpError(503)
        self.assertEqual(self.gaps(10), 10)
        self.h.net.errors[ONLINE] = FeedError("request timed out")
        self.assertEqual(self.gaps(10), 10)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.manager.set_in_schedule(True)
        self.h.run_for(1)

    def test_starts_on_the_receiver_without_contacting_online(self):
        self.h.run_for(30)
        self.assertEqual(self.h.manager.state().active_feed, FEED_RECEIVER)
        self.assertEqual(self.h.net.count(ONLINE), 0)

    def test_health_check_runs_every_10_seconds(self):
        self.h.net.calls.clear()
        with mock.patch.object(self.h.manager, "poll_once"):
            self.h.run_for(30)
        self.assertEqual(self.h.net.count(RX), 3)

    def test_one_failed_check_does_not_switch(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(10)
        self.assertEqual(self.h.manager.state().active_feed, FEED_RECEIVER)
        self.assertNotIn("receiver_lost", self.h.events.names)

    def test_two_failed_checks_switch_to_online_and_warn(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(20)
        state = self.h.manager.state()
        self.assertEqual(state.active_feed, FEED_ONLINE)
        self.assertFalse(state.receiver_ok)
        self.assertIn("receiver_lost", self.h.events.names)
        self.assertIn("fallback_active", self.h.events.names)
        self.assertEqual(len(self.h.warnings.warnings()), 2)

    def test_online_is_polled_at_once_and_delivers(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(21)
        self.assertEqual(self.h.hexes(), ["c3"])
        self.assertGreaterEqual(self.h.net.count(ONLINE), 1)

    def test_fail_count_is_a_setting(self):
        h = Harness(health_fail_count=3)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(20)
        self.assertEqual(h.manager.state().active_feed, FEED_RECEIVER)
        h.run_for(10)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)

    def test_health_interval_is_a_setting(self):
        h = Harness(health_interval_s=5.0)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(10)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)

    def test_a_good_check_resets_the_failure_count(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(10)
        del self.h.net.errors[RX]
        self.h.run_for(10)
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(10)
        self.assertEqual(self.h.manager.state().active_feed, FEED_RECEIVER)

    def test_health_checks_continue_on_online_and_switch_back(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(25)
        del self.h.net.errors[RX]
        self.h.run_for(10)
        state = self.h.manager.state()
        self.assertEqual(state.active_feed, FEED_RECEIVER)
        self.assertTrue(state.receiver_ok)
        self.assertIn("receiver_restored", self.h.events.names)
        self.assertEqual(self.h.warnings.warnings(), [])
        self.h.run_for(1)
        self.assertEqual(self.h.hexes(), ["a1"])

    def test_online_stops_after_switching_back(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(25)
        del self.h.net.errors[RX]
        self.h.run_for(10)
        before = self.h.net.count(ONLINE)
        self.h.run_for(30)
        self.assertEqual(self.h.net.count(ONLINE), before)

    def test_outside_the_window_fallback_uses_the_slow_rate(self):
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(20)
        before = h.net.count(ONLINE)
        h.run_for(29)
        self.assertLessEqual(h.net.count(ONLINE) - before, 1)

    def test_no_fallback_while_disabled(self):
        h = Harness(enabled=False)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(60)
        state = h.manager.state()
        self.assertEqual(state.active_feed, FEED_RECEIVER)
        self.assertEqual(h.net.count(ONLINE), 0)
        self.assertIn("receiver_lost", h.events.names)
        self.assertNotIn("fallback_active", h.events.names)

    def test_enabling_with_the_receiver_down_falls_back_at_once(self):
        h = Harness(enabled=False)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(30)
        h.manager.update_settings(settings())
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)
        self.assertIn("fallback_active", h.events.names)

    def test_disabling_during_fallback_stops_online_and_clears_that_warning(self):
        self.h.net.errors[RX] = FeedError("down")
        self.h.run_for(25)
        before = self.h.net.count(ONLINE)
        self.h.manager.update_settings(settings(enabled=False))
        self.h.run_for(60)
        self.assertEqual(self.h.net.count(ONLINE), before)
        self.assertIn("fallback_ended", self.h.events.names)
        remaining = " ".join(self.h.warnings.warnings())
        self.assertNotIn("online feed", remaining)
        self.assertIn("receiver", remaining)

    def test_both_mode_with_no_receiver_url_just_uses_online(self):
        h = Harness(receiver_url="")
        h.run_for(1)
        state = h.manager.state()
        self.assertEqual(state.active_feed, FEED_ONLINE)
        self.assertIsNone(state.receiver_ok)
        self.assertFalse(h.manager.health_once())
        self.assertEqual(h.events.names, [])


class FrozenFeedTests(unittest.TestCase):
    def test_frozen_file_stops_counting_as_success(self):
        h = Harness(source_mode="receiver")
        h.run_for(3)
        h.net.frozen.add(RX)
        h.run_for(1)
        success_at = h.manager.state().last_success_at
        h.run_for(10)
        self.assertEqual(h.manager.state().last_success_at, success_at)
        self.assertFalse(h.manager.data_fresh())

    def test_an_unchanged_now_is_not_a_failure_within_the_grace_period(self):
        h = Harness(source_mode="receiver")
        h.run_for(3)
        h.net.frozen.add(RX)
        h.run_for(int(FROZEN_GRACE_S))
        self.assertFalse(h.manager.state().last_poll_failed)

    def test_a_frozen_file_becomes_a_failed_poll_after_the_grace_period(self):
        h = Harness(source_mode="receiver")
        h.run_for(3)
        h.net.frozen.add(RX)
        h.run_for(int(FROZEN_GRACE_S) + 2)
        self.assertTrue(h.manager.state().last_poll_failed)

    def test_frozen_file_fails_health_checks_and_falls_back(self):
        h = Harness()
        h.run_for(1)
        h.net.frozen.add(RX)
        h.run_for(20)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)

    def test_frozen_file_does_not_switch_back(self):
        h = Harness()
        h.run_for(1)
        h.net.frozen.add(RX)
        h.run_for(60)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)

    def test_resuming_counts_as_success_and_recovers(self):
        h = Harness()
        h.run_for(1)
        h.net.frozen.add(RX)
        h.run_for(25)
        h.net.frozen.clear()
        h.run_for(10)
        self.assertEqual(h.manager.state().active_feed, FEED_RECEIVER)

    def test_a_clock_that_goes_backwards_still_counts_as_moving(self):
        h = Harness(source_mode="receiver")
        h.run_for(3)
        h.net.nows[RX] = 10.0
        h.run_for(1)
        self.assertEqual(h.manager.state().last_success_at, h.time)

    def test_stale_aircraft_list_is_kept_for_status_not_replaced(self):
        h = Harness(source_mode="receiver")
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(10)
        self.assertEqual(h.hexes(), ["a1"])


class DataAgeTests(unittest.TestCase):
    def test_no_poll_yet_is_not_fresh(self):
        self.assertFalse(Harness().manager.data_fresh())

    def test_fresh_until_max_age_then_stale(self):
        h = Harness(source_mode="receiver")
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(5)
        self.assertTrue(h.manager.data_fresh())
        h.run_for(1)
        self.assertFalse(h.manager.data_fresh())

    def test_max_age_zero_is_stale_on_the_first_failed_poll(self):
        h = Harness(source_mode="receiver", max_data_age_s=0.0)
        h.run_for(1)
        self.assertTrue(h.manager.data_fresh())
        h.net.errors[RX] = FeedError("down")
        h.run_for(1)
        self.assertFalse(h.manager.data_fresh())

    def test_recovers_when_data_returns(self):
        h = Harness(source_mode="receiver")
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(10)
        del h.net.errors[RX]
        h.run_for(1)
        self.assertTrue(h.manager.data_fresh())

    def test_either_feed_can_end_the_outage(self):
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(10)
        self.assertFalse(h.manager.data_fresh())
        h.run_for(11)  # fell back; online has delivered
        self.assertTrue(h.manager.data_fresh())

    def test_a_switch_does_not_make_old_data_look_fresh(self):
        h = Harness(max_data_age_s=0.0)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.net.errors[ONLINE] = FeedError("down")
        h.run_for(20)
        self.assertFalse(h.manager.data_fresh())


class AllFeedsDownTests(unittest.TestCase):
    def test_receiver_only_outage_warns_once_after_max_age(self):
        h = Harness(source_mode="receiver")
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(5)
        self.assertNotIn("all_feeds_down", h.events.names)
        h.run_for(30)
        self.assertEqual(h.events.names.count("all_feeds_down"), 1)

    def test_online_only_outage_warns_and_recovers(self):
        h = Harness(source_mode="online")
        h.manager.set_in_schedule(True)
        h.run_for(1)
        h.net.errors[ONLINE] = FeedError("down")
        h.run_for(10)
        self.assertIn("all_feeds_down", h.events.names)
        del h.net.errors[ONLINE]
        h.run_for(1)
        self.assertIn("feeds_restored", h.events.names)
        self.assertEqual(h.warnings.warnings(), [])

    def test_both_down_warns_and_holds(self):
        h = Harness()
        h.manager.set_in_schedule(True)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.net.errors[ONLINE] = FeedError("down")
        h.run_for(40)
        self.assertEqual(h.events.names.count("all_feeds_down"), 1)
        self.assertFalse(h.manager.data_fresh())

    def test_switching_to_an_untried_online_feed_is_not_all_down(self):
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.net.hook = lambda url: self.assertNotIn("all_feeds_down", h.events.names)
        h.run_for(21)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)
        self.assertNotIn("all_feeds_down", h.events.names)

    def test_receiver_down_with_online_still_untried_is_not_all_down(self):
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(15)
        self.assertFalse(h.manager.data_fresh())
        self.assertNotIn("all_feeds_down", h.events.names)

    def test_receiver_returning_ends_a_both_down_outage(self):
        h = Harness()
        h.manager.set_in_schedule(True)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.net.errors[ONLINE] = FeedError("down")
        h.run_for(40)
        del h.net.errors[RX]
        h.run_for(11)
        self.assertIn("feeds_restored", h.events.names)
        self.assertTrue(h.manager.data_fresh())
        self.assertEqual(h.warnings.warnings(), [])

    def test_a_successful_idle_online_poll_is_not_an_outage(self):
        h = Harness(source_mode="online")
        h.run_for(120)
        self.assertNotIn("all_feeds_down", h.events.names)

    def test_disabled_online_is_not_an_outage(self):
        h = Harness(source_mode="online", enabled=False)
        h.run_for(60)
        self.assertEqual(h.events.names, [])

    def test_disabling_clears_an_online_outage_warning(self):
        h = Harness(source_mode="online")
        h.manager.set_in_schedule(True)
        h.run_for(1)
        h.net.errors[ONLINE] = FeedError("down")
        h.run_for(10)
        h.manager.update_settings(settings(source_mode="online", enabled=False))
        self.assertEqual(h.warnings.warnings(), [])

    def test_receiver_mode_with_no_url_reports_an_outage_without_any_request(self):
        h = Harness(source_mode="receiver", receiver_url="")
        h.run_for(10)
        self.assertEqual(h.net.calls, [])
        self.assertIn("all_feeds_down", h.events.names)
        self.assertFalse(h.manager.data_fresh())


class SecondDecoderTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(source_mode="receiver", receiver_978_url=RX978)

    def test_lists_are_combined(self):
        self.h.manager.poll_second_once()
        self.h.run_for(1)
        self.assertEqual(self.h.hexes(), ["a1", "b2"])

    def test_primary_data_still_flows_when_978_is_down(self):
        self.h.net.errors[RX978] = FeedError("down")
        self.h.run_for(5)
        self.assertEqual(self.h.hexes(), ["a1"])
        self.assertTrue(self.h.manager.data_fresh())

    def test_978_outage_warns_once_after_the_health_window(self):
        self.h.run_for(1)
        self.h.net.errors[RX978] = FeedError("down")
        self.h.run_for(15)
        self.assertNotIn("receiver_978_lost", self.h.events.names)
        self.h.run_for(60)
        self.assertEqual(self.h.events.names.count("receiver_978_lost"), 1)
        self.assertEqual(len(self.h.warnings.warnings()), 1)

    def test_978_recovery_clears_the_warning(self):
        self.h.run_for(1)
        self.h.net.errors[RX978] = FeedError("down")
        self.h.run_for(30)
        del self.h.net.errors[RX978]
        self.h.run_for(2)
        self.assertEqual(self.h.warnings.warnings(), [])

    def test_stale_978_aircraft_age_out_of_the_list(self):
        self.h.run_for(2)
        self.assertIn("b2", self.h.hexes())
        self.h.net.errors[RX978] = FeedError("down")
        self.h.run_for(10)
        self.assertEqual(self.h.hexes(), ["a1"])

    def test_frozen_978_warns_too(self):
        self.h.run_for(2)
        self.h.net.frozen.add(RX978)
        self.h.run_for(40)
        self.assertIn("receiver_978_lost", self.h.events.names)
        self.assertEqual(self.h.hexes(), ["a1"])

    def test_978_failure_never_counts_against_the_primary(self):
        h = Harness(receiver_978_url=RX978)
        h.net.errors[RX978] = FeedError("down")
        h.run_for(60)
        self.assertEqual(h.manager.state().active_feed, FEED_RECEIVER)

    def test_978_is_not_polled_while_on_online(self):
        h = Harness(receiver_978_url=RX978)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(20)
        h.net.calls.clear()
        h.run_for(5)
        self.assertEqual(h.net.count(RX978), 0)

    def test_a_978_url_alone_acts_as_the_receiver(self):
        h = Harness(source_mode="receiver", receiver_url="", receiver_978_url=RX978)
        h.run_for(1)
        self.assertEqual(h.hexes(), ["b2"])
        self.assertEqual(h.manager.state().aircraft[0].source, "receiver_978")
        self.assertFalse(h.manager.poll_second_once())


class ChangesInFlightTests(unittest.TestCase):
    def test_a_result_arriving_after_disable_is_discarded(self):
        h = Harness(source_mode="online")
        h.net.hook = lambda url: h.manager.update_settings(settings(source_mode="online", enabled=False))
        h.manager.poll_once()
        state = h.manager.state()
        self.assertIsNone(state.last_success_at)
        self.assertEqual(state.aircraft, ())

    def test_a_result_from_the_old_feed_is_discarded_after_a_switch(self):
        h = Harness()
        h.run_for(1)
        success_at = h.manager.state().last_success_at

        def switch(url):
            h.net.hook = None
            h.manager.update_settings(settings(source_mode="online"))

        h.net.hook = switch
        h.time += 1
        h.manager.poll_once()
        self.assertEqual(h.manager.state().last_success_at, success_at)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)

    def test_no_lock_is_held_during_a_request(self):
        h = Harness(source_mode="receiver")
        results = []

        def try_lock():
            # An RLock can be re-acquired by its owner, so this must run on another thread.
            acquired = h.manager._lock.acquire(blocking=False)
            results.append(acquired)
            if acquired:
                h.manager._lock.release()

        def probe(url):
            worker = threading.Thread(target=try_lock)
            worker.start()
            worker.join()

        h.net.hook = probe
        h.manager.poll_once()
        self.assertEqual(results, [True])


class HealthResultTests(unittest.TestCase):
    def test_a_health_result_survives_a_feed_switch_during_the_check(self):
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(10)
        h.net.hook = lambda url: h.manager._refresh_active()
        h.run_for(10)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)

    def test_a_health_result_is_discarded_after_leaving_both_mode(self):
        h = Harness(health_fail_count=1)
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")

        def leave(url):
            h.net.hook = None
            h.manager.update_settings(settings(source_mode="receiver", health_fail_count=1))

        h.net.hook = leave
        h.time += 10
        h.manager.health_once()
        self.assertEqual(h.events.names.count("receiver_lost"), 0)


class SettingsChangeTests(unittest.TestCase):
    def test_changing_the_center_polls_at_once(self):
        h = Harness(source_mode="online")
        h.run_for(1)
        h.manager.update_settings(settings(source_mode="online", center_lat=41.0))
        h.manager.poll_once()
        self.assertIn("/41.0000/", h.net.calls[-1])

    def test_changing_mode_clears_receiver_warnings(self):
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(25)
        self.assertTrue(h.warnings.warnings())
        h.manager.update_settings(settings(source_mode="online"))
        self.assertEqual(h.warnings.warnings(), [])

    def test_changing_the_receiver_url_gives_it_a_fresh_start(self):
        other = "http://other/aircraft.json"
        h = Harness()
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(25)
        h.manager.update_settings(settings(receiver_url=other))
        self.assertEqual(h.manager.state().active_feed, FEED_RECEIVER)
        self.assertEqual(h.warnings.warnings(), [])

    def test_a_settings_change_keeps_old_data_stale(self):
        h = Harness(source_mode="receiver")
        h.run_for(1)
        h.net.errors[RX] = FeedError("down")
        h.run_for(10)
        h.manager.update_settings(settings(source_mode="receiver", radius=20.0))
        self.assertFalse(h.manager.data_fresh())

    def test_unrelated_changes_do_not_force_a_poll(self):
        h = Harness(source_mode="online")
        h.run_for(1)
        h.manager.update_settings(settings(source_mode="online", release_delay_s=9.0))
        h.manager.poll_once()
        self.assertEqual(h.net.count(ONLINE), 1)


class PrivacyTests(unittest.TestCase):
    def test_logs_never_contain_the_url_or_coordinates(self):
        h = Harness(source_mode="online", center_lat=12.3456, center_lon=-65.4321)
        h.manager.set_in_schedule(True)
        h.net.errors[ONLINE] = FeedHttpError(429)
        with self.assertLogs("skyguard", level="WARNING") as captured:
            h.run_for(3)
        text = "\n".join(captured.output)
        self.assertIn("HTTP 429", text)
        for private in ("12.3456", "65.4321", "adsb.lol/v2"):
            self.assertNotIn(private, text)

    def test_a_repeating_failure_logs_once(self):
        h = Harness(source_mode="receiver")
        h.net.errors[RX] = FeedError("connection failed")
        with self.assertLogs("skyguard", level="WARNING") as captured:
            h.run_for(20)
        self.assertEqual(sum("poll failed" in line for line in captured.output), 1)

    def test_recovery_is_logged(self):
        h = Harness(source_mode="receiver")
        h.net.errors[RX] = FeedError("connection failed")
        h.run_for(2)
        del h.net.errors[RX]
        with self.assertLogs("skyguard", level="INFO") as captured:
            h.run_for(1)
        self.assertTrue(any("delivering data again" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
