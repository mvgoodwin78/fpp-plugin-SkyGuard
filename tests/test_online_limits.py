"""Rules that came from testing on a real box: provider rates, data age, Retry-After, the second
radius, the lag notice and the personal-use confirmation."""

import json
import os
import shutil
import tempfile
import unittest

from skyguard.config import Paths, Settings, load_settings
from skyguard.daemon import ONLINE_LAG_NOTICE, Daemon
from skyguard.feed_manager import FEED_ONLINE, FEED_RECEIVER, FeedManager
from skyguard.feeds.base import FeedError
from skyguard.feeds.http_client import FeedHttpError, fetch_json
from skyguard.feeds.online_adsbx_v2 import DEFAULT_PROVIDER, PROVIDERS
from skyguard.fpp_api import FppApi
from skyguard.geo import to_nm
from skyguard.notifier import Notifier, WarningSink
from tests.fake_fpp_server import FakeFpp
from tests.mock_feed_server import MockFeedServer

RX = "http://rx/aircraft.json"
CENTER = {"center_lat": 40.0, "center_lon": -75.0}
ZONE = {**CENTER, "radius": 10.0}


class Net:
    def __init__(self):
        self.calls = []
        self.error = None
        self.now = 1000.0

    def fetch(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        self.now += 1
        return {"ac": [], "msg": "No error", "now": self.now * 1000}


class Rig:
    def __init__(self, **values):
        self.time = 0.0
        self.net = Net()
        self.sink = WarningSink()
        settings = {
            "source_mode": "online",
            "personal_use_confirmed": True,
            **ZONE,
            **values,
        }
        self.manager = FeedManager(
            Settings(**settings), Notifier([self.sink]), fetch=self.net.fetch, clock=lambda: self.time
        )
        self.manager.set_in_schedule(True)

    def run_for(self, seconds):
        for _ in range(int(seconds)):
            self.time += 1
            self.manager.poll_once()


class ProviderRateTests(unittest.TestCase):
    def test_the_default_is_adsb_fi(self):
        self.assertEqual(DEFAULT_PROVIDER, "adsb.fi")
        self.assertEqual(Settings().online_provider, "adsb.fi")

    def test_minimums_match_what_was_measured(self):
        self.assertEqual(PROVIDERS["adsb.fi"].min_interval_s, 2.0)
        self.assertEqual(PROVIDERS["adsb.lol"].min_interval_s, 15.0)

    def test_adsb_fi_polls_every_2_seconds_during_a_show(self):
        rig = Rig(online_provider="adsb.fi")
        rig.run_for(20)
        self.assertEqual(len(rig.net.calls), 10)

    def test_adsb_lol_polls_every_15_seconds_during_a_show(self):
        rig = Rig(online_provider="adsb.lol")
        rig.run_for(60)
        self.assertEqual(len(rig.net.calls), 4)

    def test_adsb_lol_never_polls_faster_than_it_refused(self):
        rig = Rig(online_provider="adsb.lol", poll_interval_s=1.0)
        rig.run_for(300)
        self.assertLessEqual(len(rig.net.calls), 300 / 10)


class DataAgeTests(unittest.TestCase):
    def test_adsb_lol_stays_fresh_between_its_polls(self):
        rig = Rig(online_provider="adsb.lol")
        for _ in range(120):
            rig.time += 1
            rig.manager.poll_once()
            if rig.time > 1:
                self.assertTrue(rig.manager.data_fresh(), f"stale at {rig.time}")

    def test_adsb_lol_goes_stale_after_two_intervals(self):
        rig = Rig(online_provider="adsb.lol")
        rig.run_for(1)
        rig.net.error = FeedError("down")
        rig.run_for(30)
        self.assertTrue(rig.manager.data_fresh())
        rig.run_for(1)
        self.assertFalse(rig.manager.data_fresh())

    def test_adsb_fi_keeps_the_5_second_limit(self):
        rig = Rig(online_provider="adsb.fi")
        rig.run_for(1)
        rig.net.error = FeedError("down")
        rig.run_for(5)
        self.assertTrue(rig.manager.data_fresh())
        rig.run_for(1)
        self.assertFalse(rig.manager.data_fresh())

    def test_a_longer_user_limit_is_kept(self):
        rig = Rig(online_provider="adsb.fi", max_data_age_s=20.0)
        rig.run_for(1)
        rig.net.error = FeedError("down")
        rig.run_for(20)
        self.assertTrue(rig.manager.data_fresh())

    def test_zero_still_means_the_first_failed_poll(self):
        rig = Rig(online_provider="adsb.lol", max_data_age_s=0.0)
        rig.run_for(1)
        rig.net.error = FeedError("down")
        rig.run_for(15)
        self.assertFalse(rig.manager.data_fresh())

    def test_a_receiver_keeps_the_setting_itself(self):
        rig = Rig(source_mode="receiver", receiver_url=RX, online_provider="adsb.lol")
        rig.net.fetch = lambda url: {"now": rig.time, "aircraft": []}
        rig.manager._fetch = rig.net.fetch
        rig.run_for(2)
        rig.manager._fetch = lambda url: (_ for _ in ()).throw(FeedError("down"))
        rig.run_for(6)
        self.assertFalse(rig.manager.data_fresh())


class RetryAfterTests(unittest.TestCase):
    def test_no_request_is_made_until_the_wait_is_over(self):
        rig = Rig(online_provider="adsb.fi")
        rig.run_for(1)
        rig.net.error = FeedHttpError(429, retry_after=20)
        rig.run_for(2)
        count = len(rig.net.calls)
        rig.net.error = None
        rig.run_for(19)
        self.assertEqual(len(rig.net.calls), count)
        rig.run_for(2)
        self.assertGreater(len(rig.net.calls), count)

    def test_it_beats_the_poll_at_show_start(self):
        rig = Rig(online_provider="adsb.fi")
        rig.run_for(1)
        rig.net.error = FeedHttpError(429, retry_after=20)
        rig.run_for(2)
        count = len(rig.net.calls)
        rig.manager.set_in_schedule(False)
        rig.manager.set_in_schedule(True)
        rig.manager.poll_once()
        self.assertEqual(len(rig.net.calls), count)

    def test_changing_provider_clears_the_wait(self):
        rig = Rig(online_provider="adsb.fi")
        rig.run_for(1)
        rig.net.error = FeedHttpError(429, retry_after=200)
        rig.run_for(2)
        rig.net.error = None
        count = len(rig.net.calls)
        rig.manager.update_settings(
            Settings(source_mode="online", personal_use_confirmed=True, online_provider="adsb.lol", **ZONE)
        )
        rig.manager.poll_once()
        self.assertEqual(len(rig.net.calls), count + 1)

    def test_a_refusal_without_it_only_backs_off(self):
        rig = Rig(online_provider="adsb.fi")
        rig.net.error = FeedHttpError(429)
        rig.run_for(30)
        self.assertGreater(len(rig.net.calls), 1)

    def test_the_header_is_read_from_a_real_response(self):
        server = MockFeedServer().start()
        self.addCleanup(server.stop)
        route = server.add("/x", [{}])
        route.status = 429
        route.headers = {"Retry-After": "3"}
        with self.assertRaises(FeedHttpError) as caught:
            fetch_json(server.url("/x"))
        self.assertEqual(caught.exception.retry_after, 3)

    def test_a_huge_or_odd_header_is_handled(self):
        server = MockFeedServer().start()
        self.addCleanup(server.stop)
        route = server.add("/x", [{}])
        route.status = 429
        for value, expected in (("999999", 300), ("soon", None), ("", None)):
            route.headers = {"Retry-After": value}
            with self.assertRaises(FeedHttpError) as caught:
                fetch_json(server.url("/x"))
            self.assertEqual(caught.exception.retry_after, expected, value)


class RadiusSettingTests(unittest.TestCase):
    def test_neither_radius_has_a_default_or_borrows_the_other(self):
        self.assertIsNone(Settings().radius)
        self.assertIsNone(Settings().radius_receiver)
        online_only = load_settings({"radius": "12"})
        self.assertIsNone(online_only.radius_for(FEED_RECEIVER))
        self.assertEqual(online_only.radius_for(FEED_ONLINE), 12.0)
        receiver_only = load_settings({"radius_receiver": "9"})
        self.assertIsNone(receiver_only.radius_for(FEED_ONLINE))
        self.assertEqual(receiver_only.radius_for(FEED_RECEIVER), 9.0)
        self.assertEqual(online_only.warnings, ())

    def test_which_radii_are_required_depends_on_the_sources_in_use(self):
        for values, expected in (
            ({"source_mode": "online"}, False),
            ({"source_mode": "online", "radius": "13"}, True),
            ({"source_mode": "online", "radius_receiver": "10"}, False),
            ({"source_mode": "receiver"}, False),
            ({"source_mode": "receiver", "radius_receiver": "10"}, True),
            ({"source_mode": "receiver", "radius": "13"}, False),
            ({"source_mode": "both", "receiver_url": RX, "radius": "13"}, False),
            ({"source_mode": "both", "receiver_url": RX, "radius_receiver": "10"}, False),
            ({"source_mode": "both", "receiver_url": RX, "radius": "13", "radius_receiver": "10"}, True),
            ({"source_mode": "both", "radius": "13"}, True),  # no receiver entered, so online only
        ):
            with self.subTest(values=values):
                self.assertEqual(load_settings(values).radius_ready, expected)

    def test_a_missing_radius_gives_no_crash_on_conversion(self):
        self.assertIsNone(Settings().radius_nm)
        self.assertIsNone(Settings().radius_nm_for(FEED_RECEIVER))

    def test_each_feed_gets_its_own(self):
        s = load_settings({"radius": "13", "radius_receiver": "10"})
        self.assertEqual(s.radius_for(FEED_RECEIVER), 10.0)
        self.assertEqual(s.radius_for(FEED_ONLINE), 13.0)

    def test_the_unit_applies_to_both(self):
        s = load_settings({"radius": "20", "radius_receiver": "10", "radius_unit": "km"})
        self.assertAlmostEqual(s.radius_nm_for(FEED_RECEIVER), to_nm(10, "km"))
        self.assertAlmostEqual(s.radius_nm_for(FEED_ONLINE), to_nm(20, "km"))

    def test_online_queries_use_the_online_radius(self):
        s = load_settings({"radius": "13", "radius_receiver": "1", "radius_unit": "nm"})
        self.assertEqual(s.radius_nm, 13.0)

    def test_invalid_falls_back_to_none_with_a_warning(self):
        for bad in ("0", "-1", "abc"):
            s = load_settings({"radius_receiver": bad})
            self.assertIsNone(s.radius_receiver, bad)
            self.assertTrue(any("radius_receiver" in w for w in s.warnings))

    def test_an_invalid_main_radius_is_treated_as_not_set(self):
        s = load_settings({"radius": "abc"})
        self.assertIsNone(s.radius)
        self.assertTrue(any("'radius'" in w and "not set" in w for w in s.warnings))

    def test_blank_means_not_set(self):
        s = load_settings({"radius_receiver": ""})
        self.assertIsNone(s.radius_receiver)
        self.assertEqual(s.warnings, ())


class PersonalUseFeedTests(unittest.TestCase):
    def test_it_starts_unconfirmed(self):
        self.assertFalse(Settings().personal_use_confirmed)
        self.assertFalse(load_settings({}).personal_use_confirmed)

    def test_no_online_requests_until_confirmed(self):
        rig = Rig(personal_use_confirmed=False)
        rig.run_for(60)
        self.assertEqual(rig.net.calls, [])
        self.assertFalse(rig.manager.state().polling)

    def test_confirming_starts_polling(self):
        rig = Rig(personal_use_confirmed=False)
        rig.run_for(5)
        rig.manager.update_settings(Settings(source_mode="online", personal_use_confirmed=True, **ZONE))
        rig.manager.poll_once()
        self.assertEqual(len(rig.net.calls), 1)

    def test_withdrawing_it_discards_a_request_in_flight(self):
        rig = Rig()
        real = rig.net.fetch

        def withdraw(url):
            rig.manager.update_settings(Settings(source_mode="online", personal_use_confirmed=False, **ZONE))
            return real(url)

        rig.manager._fetch = withdraw
        rig.time += 1
        rig.manager.poll_once()
        self.assertIsNone(rig.manager.state().last_success_at)

    def test_both_mode_has_no_fallback_until_confirmed(self):
        rig = Rig(source_mode="both", receiver_url=RX, personal_use_confirmed=False, health_interval_s=1.0)
        rig.manager._fetch = lambda url: (_ for _ in ()).throw(FeedError("down"))
        for _ in range(30):
            rig.time += 1
            rig.manager.poll_once()
            rig.manager.health_once()
        self.assertEqual(rig.manager.state().active_feed, FEED_RECEIVER)
        self.assertFalse(any("adsb" in url for url in rig.net.calls))

    def test_receiver_polling_continues_so_status_stays_useful(self):
        rig = Rig(source_mode="receiver", receiver_url=RX, personal_use_confirmed=False)
        rig.manager._fetch = lambda url: {"now": rig.time, "aircraft": []}
        rig.run_for(3)
        self.assertTrue(rig.manager.data_fresh())


class DaemonRig:
    def __init__(self, test, settings, private=None, default_radii=True):
        self.time = 1000.0
        self.dir = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.paths = Paths.from_env({"MEDIADIR": self.dir})
        os.makedirs(os.path.dirname(self.paths.config_file))
        os.makedirs(self.paths.data_dir)
        with open(self.paths.config_file, "w", encoding="utf-8") as handle:
            radii = {"radius": "10", "radius_receiver": "10"} if default_radii else {}
            for key, value in {**radii, **settings}.items():
                handle.write(f'{key} = "{value}"\n')
        with open(self.paths.private_file, "w", encoding="utf-8") as handle:
            json.dump(private if private is not None else CENTER, handle)
        self.fpp = FakeFpp()
        test.addCleanup(self.fpp.stop)
        self.fpp.status = {"status": 1, "status_name": "playing"}
        self.aircraft = []
        self.receiver_down = False
        self.online_calls = 0
        self.receiver_now = 0.0
        self.online_now = 0.0
        self.daemon = Daemon(
            self.paths,
            FppApi(self.fpp.url, timeout_s=1.0),
            fetch=self.fetch,
            clock=lambda: self.time,
            wall_clock=lambda: 1793900000.0 + self.time,
        )

    def fetch(self, url):
        if url.startswith("https://"):
            self.online_calls += 1
            self.online_now += 1
            return {"ac": self.aircraft, "msg": "No error", "now": self.online_now * 1000}
        if self.receiver_down:
            raise FeedError("down")
        self.receiver_now += 1
        return {"now": self.receiver_now, "aircraft": self.aircraft}

    def tick(self, steps=1):
        for _ in range(steps):
            self.time += 1
            self.daemon.fpp_watch.poll_once()
            self.daemon.feed.set_in_schedule(self.daemon.fpp_watch.in_show(self.time))
            self.daemon.feed.poll_once()
            self.daemon.feed.health_once()
            self.daemon.step()

    def status(self):
        self.time += 5
        self.daemon.step()
        with open(self.paths.status_file, encoding="utf-8") as handle:
            return json.load(handle)


class SecondRadiusDaemonTests(unittest.TestCase):
    # About 11.5 statute miles north of center: inside 13 mi, outside 10 mi.
    BETWEEN = {"hex": "a1", "alt_baro": 3000, "gs": 120.0, "seen_pos": 0.5, "lat": 40.1667, "lon": -75.0}
    SETTINGS = {
        "source_mode": "both",
        "receiver_url": RX,
        "radius": "13",
        "radius_receiver": "10",
        "personal_use_confirmed": "1",
    }

    def test_the_receiver_uses_its_radius(self):
        rig = DaemonRig(self, self.SETTINGS)
        rig.aircraft = [self.BETWEEN]
        rig.tick(2)
        self.assertEqual(rig.daemon.engine.state, "CLEAR")

    def test_falling_back_to_online_uses_the_larger_radius(self):
        rig = DaemonRig(self, self.SETTINGS)
        rig.aircraft = [self.BETWEEN]
        rig.tick(2)
        rig.receiver_down = True
        rig.tick(30)
        self.assertEqual(rig.daemon.feed.state().active_feed, FEED_ONLINE)
        self.assertEqual((rig.daemon.engine.state, rig.daemon.engine.reason), ("BLACKOUT", "aircraft"))

    def test_status_shows_the_radius_in_force(self):
        rig = DaemonRig(self, self.SETTINGS)
        rig.tick(2)
        self.assertEqual(
            rig.status()["radius"], {"value": 10.0, "unit": "mi", "online": 13.0, "receiver": 10.0}
        )
        rig.receiver_down = True
        rig.tick(30)
        self.assertEqual(rig.status()["radius"]["value"], 13.0)

    def test_the_status_list_follows_the_radius_in_force(self):
        # 11.5 mi away: beyond twice a 1 mi receiver radius, within twice the 13 mi online one.
        rig = DaemonRig(self, {**self.SETTINGS, "radius_receiver": "1"})
        rig.aircraft = [self.BETWEEN]
        rig.tick(2)
        self.assertEqual(rig.status()["aircraft"], [])

    def test_online_queries_cover_the_online_radius_even_if_the_receiver_one_is_small(self):
        rig = DaemonRig(self, {**self.SETTINGS, "radius_receiver": "1", "source_mode": "online"})
        calls = []
        real = rig.fetch

        def spy(url):
            calls.append(url)
            return real(url)

        rig.daemon.feed._fetch = spy
        rig.tick(1)
        self.assertTrue(calls[0].endswith("/dist/23"), calls[0])  # twice 13 mi, in nm, rounded up


class MissingRadiusTests(unittest.TestCase):
    INSIDE = {"hex": "a1", "alt_baro": 3000, "gs": 120.0, "seen_pos": 0.5, "lat": 40.05, "lon": -75.0}

    def rig(self, values):
        rig = DaemonRig(self, values, default_radii=False)
        rig.aircraft = [self.INSIDE]
        return rig

    def test_a_missing_radius_stays_idle_with_a_warning(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick(2)
        self.assertEqual((rig.daemon.engine.state, rig.daemon.engine.reason), ("IDLE", "no_radius"))
        self.assertEqual(rig.fpp.ranges, {})
        self.assertTrue(any("radius" in w for w in rig.status()["warnings"]))

    def test_both_mode_needs_both(self):
        rig = self.rig({"source_mode": "both", "receiver_url": RX, "radius": "13", "personal_use_confirmed": "1"})
        rig.tick(2)
        self.assertEqual(rig.daemon.engine.reason, "no_radius")

    def test_entering_it_starts_protection_and_clears_the_warning(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick(2)
        with open(rig.paths.config_file, "a", encoding="utf-8") as handle:
            handle.write('radius_receiver = "10"\n')
        info = os.stat(rig.paths.config_file)
        os.utime(rig.paths.config_file, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        rig.tick(2)
        self.assertEqual((rig.daemon.engine.state, rig.daemon.engine.reason), ("BLACKOUT", "aircraft"))
        self.assertFalse(any("radius" in w for w in rig.status()["warnings"]))

    def test_no_online_requests_without_an_online_radius(self):
        rig = self.rig({"source_mode": "online", "personal_use_confirmed": "1"})
        rig.tick(5)
        self.assertEqual(rig.online_calls, 0)

    def test_status_is_still_usable(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick(2)
        status = rig.status()
        self.assertEqual(status["radius"], {"value": None, "unit": "mi", "online": None, "receiver": None})
        self.assertEqual(status["aircraft"], [])

    def test_a_test_blackout_still_works(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick()
        rig.daemon.engine.start_test(rig.time, 5)
        rig.tick()
        self.assertEqual(rig.daemon.engine.reason, "test")


class NoticeAndConfirmationDaemonTests(unittest.TestCase):
    def test_the_lag_notice_shows_with_online_data(self):
        for mode in ("online", "both"):
            rig = DaemonRig(self, {"source_mode": mode, "personal_use_confirmed": "1"})
            rig.tick()
            self.assertEqual(rig.status()["notices"], [ONLINE_LAG_NOTICE], mode)

    def test_it_says_to_add_3_miles(self):
        self.assertIn("3 miles", ONLINE_LAG_NOTICE)

    def test_no_lag_notice_for_a_receiver(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX})
        rig.tick()
        self.assertEqual(rig.status()["notices"], [])

    def test_the_lag_notice_is_not_a_warning(self):
        rig = DaemonRig(self, {"source_mode": "online", "personal_use_confirmed": "1"})
        rig.tick()
        self.assertNotIn(ONLINE_LAG_NOTICE, rig.status()["warnings"])

    def test_the_lag_notice_is_logged_once(self):
        with self.assertLogs("skyguard", level="INFO") as captured:
            rig = DaemonRig(self, {"source_mode": "online", "personal_use_confirmed": "1"})
            rig.tick(5)
        self.assertEqual(sum(ONLINE_LAG_NOTICE in line for line in captured.output), 1)

    def test_unconfirmed_warns_and_sends_nothing(self):
        rig = DaemonRig(self, {"source_mode": "online"})
        rig.tick(5)
        self.assertEqual(rig.online_calls, 0)
        self.assertTrue(any("personal use" in w for w in rig.status()["warnings"]))

    def test_every_setup_stays_idle_until_confirmed(self):
        for values in (
            {"source_mode": "online"},
            {"source_mode": "both", "receiver_url": RX},
            {"source_mode": "receiver", "receiver_url": RX},
        ):
            with self.subTest(values=values):
                rig = DaemonRig(self, values)
                rig.aircraft = [SecondRadiusDaemonTests.BETWEEN]
                rig.tick(3)
                engine = rig.daemon.engine
                self.assertEqual((engine.state, engine.reason), ("IDLE", "not_confirmed"))
                self.assertEqual(rig.fpp.ranges, {})
                self.assertEqual(rig.online_calls, 0)

    def test_a_test_blackout_waits_for_the_confirmation(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX})
        rig.tick()
        rig.daemon.engine.start_test(rig.time, 5)
        rig.tick()
        self.assertEqual(rig.daemon.engine.reason, "not_confirmed")
        self.assertFalse(rig.daemon.engine.test_active)
        self.assertEqual(rig.fpp.ranges, {})

    def test_it_outranks_the_other_setup_warnings_as_the_reason(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX}, private={}, default_radii=False)
        rig.tick()
        self.assertEqual(rig.daemon.engine.reason, "not_confirmed")

    def test_disabled_outranks_it(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX, "enabled": "0"})
        rig.tick()
        self.assertEqual(rig.daemon.engine.reason, "disabled")

    def test_confirming_starts_protection_without_a_restart(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX})
        rig.aircraft = [{"hex": "a1", "alt_baro": 3000, "seen_pos": 0.5, "lat": 40.05, "lon": -75.0}]
        rig.tick(2)
        with open(rig.paths.config_file, "a", encoding="utf-8") as handle:
            handle.write('personal_use_confirmed = "1"\n')
        rig.tick(2)
        self.assertEqual((rig.daemon.engine.state, rig.daemon.engine.reason), ("BLACKOUT", "aircraft"))

    def test_withdrawing_it_releases_at_once(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.aircraft = [{"hex": "a1", "alt_baro": 3000, "seen_pos": 0.5, "lat": 40.05, "lon": -75.0}]
        rig.tick(2)
        self.assertEqual(rig.daemon.engine.state, "BLACKOUT")
        with open(rig.paths.config_file, "w", encoding="utf-8") as handle:
            handle.write(
                'source_mode = "receiver"\nreceiver_url = "http://rx/aircraft.json"\n'
                'personal_use_confirmed = "0"\nradius = "10"\nradius_receiver = "10"\n'
            )
        # The same size as before, so make sure the change is seen whatever the clock's resolution.
        info = os.stat(rig.paths.config_file)
        os.utime(rig.paths.config_file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        rig.tick()
        self.assertEqual(rig.daemon.engine.reason, "not_confirmed")
        self.assertEqual(rig.fpp.ranges, {})
        self.assertEqual(rig.fpp.ever_full_on, [])

    def test_confirming_clears_the_warning_and_starts_polling(self):
        rig = DaemonRig(self, {"source_mode": "online"})
        rig.tick()
        with open(rig.paths.config_file, "a", encoding="utf-8") as handle:
            handle.write('personal_use_confirmed = "1"\n')
        rig.tick(3)
        self.assertGreater(rig.online_calls, 0)
        self.assertFalse(any("personal use" in w for w in rig.status()["warnings"]))

    def test_a_receiver_setup_gets_the_warning_too(self):
        rig = DaemonRig(self, {"source_mode": "receiver", "receiver_url": RX})
        rig.tick()
        self.assertTrue(any("personal use" in w for w in rig.status()["warnings"]))

    def test_disabled_gets_no_confirmation_warning(self):
        rig = DaemonRig(self, {"source_mode": "online", "enabled": "0"})
        rig.tick()
        self.assertFalse(any("personal use" in w for w in rig.status()["warnings"]))


if __name__ == "__main__":
    unittest.main()
