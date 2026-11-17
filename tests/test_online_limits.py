"""Rules that came from testing on a real box: provider rates, data age, Retry-After, the zones
for each feed, the lag notice and the personal-use confirmation."""

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
from tests.zones import zone_settings

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
            zone_settings(**settings), Notifier([self.sink]), fetch=self.net.fetch, clock=lambda: self.time
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
            zone_settings(source_mode="online", personal_use_confirmed=True, online_provider="adsb.lol", **ZONE)
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


def zone_keys(prefix, north, south, east, west, floor=None, ceiling=None):
    values = {f"{prefix}_north": north, f"{prefix}_south": south, f"{prefix}_east": east, f"{prefix}_west": west}
    if floor is not None:
        values[f"{prefix}_alt_floor_ft"] = floor
    if ceiling is not None:
        values[f"{prefix}_alt_ceiling_ft"] = ceiling
    return values


class ZoneSettingTests(unittest.TestCase):
    """Receiver zones 1 and 2, and the online zone: four distances and an altitude band each."""

    def test_no_zone_has_a_default_or_borrows_another(self):
        self.assertEqual(Settings().zones, ())
        online_only = load_settings(zone_keys("online_zone", "5", "5", "5", "5"))
        self.assertEqual(online_only.zones_for(FEED_RECEIVER), ())
        self.assertEqual([z.id for z in online_only.zones_for(FEED_ONLINE)], ["online"])
        receiver_only = load_settings(zone_keys("zone1", "3", "1", "2", "4"))
        self.assertEqual(receiver_only.zones_for(FEED_ONLINE), ())
        zone = receiver_only.zone("receiver1")
        self.assertEqual((zone.north, zone.south, zone.east, zone.west), (3.0, 1.0, 2.0, 4.0))
        self.assertEqual(online_only.warnings, ())

    def test_which_zones_are_required_depends_on_the_sources_in_use(self):
        online = zone_keys("online_zone", "8", "8", "8", "8")
        receiver = zone_keys("zone1", "3", "3", "3", "3")
        second = {"zone2_enabled": "1", **zone_keys("zone2", "6", "6", "6", "6")}
        for values, expected in (
            ({"source_mode": "online"}, False),
            ({"source_mode": "online", **online}, True),
            ({"source_mode": "online", **receiver}, False),
            ({"source_mode": "receiver"}, False),
            ({"source_mode": "receiver", **receiver}, True),
            ({"source_mode": "receiver", **second}, False),  # zone 2 alone isn't enough
            ({"source_mode": "receiver", **online}, False),
            ({"source_mode": "both", "receiver_url": RX, **online}, False),
            ({"source_mode": "both", "receiver_url": RX, **receiver}, False),
            ({"source_mode": "both", "receiver_url": RX, **online, **receiver}, True),
            ({"source_mode": "both", **online}, True),  # no receiver entered, so online only
        ):
            with self.subTest(values=values):
                self.assertEqual(load_settings(values).zones_ready, expected)

    def test_zone_2_is_used_only_when_switched_on(self):
        values = {"source_mode": "receiver", **zone_keys("zone1", "3", "3", "3", "3"), **zone_keys("zone2", "1", "9", "1", "1")}
        self.assertIsNone(load_settings(values).zone("receiver2"))
        on = load_settings({**values, "zone2_enabled": "1"})
        self.assertEqual([z.id for z in on.zones_for(FEED_RECEIVER)], ["receiver1", "receiver2"])

    def test_each_zone_has_its_own_altitude_band(self):
        s = load_settings({
            **zone_keys("zone1", "3", "3", "3", "3", "0", "2000"),
            "zone2_enabled": "1",
            **zone_keys("zone2", "6", "6", "6", "6", "1500", "6000"),
            **zone_keys("online_zone", "8", "8", "8", "8", "0", "9000"),
        })
        self.assertEqual(
            [(z.alt_floor_ft, z.alt_ceiling_ft) for z in s.zones],
            [(0.0, 2000.0), (1500.0, 6000.0), (0.0, 9000.0)],
        )

    def test_a_band_upside_down_uses_the_default_with_a_warning(self):
        s = load_settings(zone_keys("zone1", "3", "3", "3", "3", "5000", "1000"))
        self.assertEqual((s.zone("receiver1").alt_floor_ft, s.zone("receiver1").alt_ceiling_ft), (0.0, 10000.0))
        self.assertTrue(any("Receiver zone 1" in w for w in s.warnings))

    def test_a_missing_distance_leaves_the_zone_unset_with_a_warning(self):
        s = load_settings({"zone1_north": "3", "zone1_south": "3", "zone1_east": "3", "zone1_west": ""})
        self.assertIsNone(s.zone("receiver1"))
        self.assertTrue(any("all four distances" in w for w in s.warnings))

    def test_all_blank_means_not_set_without_a_warning(self):
        s = load_settings(zone_keys("zone1", "", "", "", ""))
        self.assertIsNone(s.zone("receiver1"))
        self.assertEqual(s.warnings, ())

    def test_a_side_can_be_zero_but_the_box_needs_area(self):
        self.assertIsNotNone(load_settings(zone_keys("zone1", "5", "0", "2", "2")).zone("receiver1"))
        flat = load_settings(zone_keys("zone1", "5", "0", "0", "0"))
        self.assertIsNone(flat.zone("receiver1"))
        self.assertTrue(any("no area" in w for w in flat.warnings))

    def test_invalid_distances_are_treated_as_not_set(self):
        for bad in ("-1", "abc", "nan", "251"):
            with self.subTest(bad=bad):
                s = load_settings(zone_keys("zone1", bad, "3", "3", "3"))
                self.assertIsNone(s.zone("receiver1"))
                self.assertTrue(any("'zone1_north'" in w and "not set" in w for w in s.warnings))

    def test_the_unit_applies_to_every_zone(self):
        s = load_settings({**zone_keys("zone1", "10", "10", "10", "10"), **zone_keys("online_zone", "20", "20", "20", "20"), "radius_unit": "km"})
        self.assertAlmostEqual(s.zone("receiver1").bounds_nm()[0], to_nm(10, "km"))
        self.assertAlmostEqual(s.zone("online").bounds_nm()[0], to_nm(20, "km"))

    def test_online_queries_reach_the_online_zones_farthest_corner(self):
        s = load_settings({**zone_keys("online_zone", "3", "1", "4", "2"), **zone_keys("zone1", "50", "50", "50", "50"), "radius_unit": "nm"})
        self.assertAlmostEqual(s.online_reach_nm, 5.0)

    def test_an_online_zone_beyond_the_query_cap_warns(self):
        s = load_settings({**zone_keys("online_zone", "200", "200", "200", "200"), "radius_unit": "nm"})
        self.assertTrue(any("farther than" in w for w in s.warnings))


class LegacyRadiusTests(unittest.TestCase):
    """Settings saved before zones were boxes keep protecting until the update's migration
    rewrites them: each radius becomes a square box of the same size."""

    def test_a_radius_becomes_a_square_box(self):
        s = load_settings({"radius": "13", "radius_receiver": "10", "alt_floor_ft": "100", "alt_ceiling_ft": "3000"})
        receiver = s.zone("receiver1")
        online = s.zone("online")
        self.assertEqual((receiver.north, receiver.south, receiver.east, receiver.west), (10.0,) * 4)
        self.assertEqual((online.north, online.south, online.east, online.west), (13.0,) * 4)
        self.assertEqual((receiver.alt_floor_ft, receiver.alt_ceiling_ft), (100.0, 3000.0))

    def test_new_keys_win_over_the_old_radius(self):
        s = load_settings({"radius_receiver": "10", **zone_keys("zone1", "2", "2", "2", "2")})
        self.assertEqual(s.zone("receiver1").north, 2.0)

    def test_an_invalid_old_radius_is_treated_as_not_set(self):
        s = load_settings({"radius": "abc"})
        self.assertIsNone(s.zone("online"))
        self.assertTrue(any("'radius'" in w and "not set" in w for w in s.warnings))

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
        rig.manager.update_settings(zone_settings(source_mode="online", personal_use_confirmed=True, **ZONE))
        rig.manager.poll_once()
        self.assertEqual(len(rig.net.calls), 1)

    def test_withdrawing_it_discards_a_request_in_flight(self):
        rig = Rig()
        real = rig.net.fetch

        def withdraw(url):
            rig.manager.update_settings(zone_settings(source_mode="online", personal_use_confirmed=False, **ZONE))
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

    def test_status_shows_the_zones_and_which_is_in_use(self):
        rig = DaemonRig(self, self.SETTINGS)
        rig.tick(2)
        status = rig.status()
        self.assertEqual(status["unit"], "mi")
        self.assertEqual(
            [(z["id"], z["north"], z["active"]) for z in status["zones"]],
            [("receiver1", 10.0, True), ("online", 13.0, False)],
        )
        rig.receiver_down = True
        rig.tick(30)
        self.assertEqual([z["active"] for z in rig.status()["zones"]], [False, True])

    def test_the_status_list_follows_the_map_range(self):
        # 11.5 mi away: beyond a 5 mi map range and the 1 mi receiver radius.
        rig = DaemonRig(self, {**self.SETTINGS, "radius_receiver": "1", "map_range": "5"})
        rig.aircraft = [self.BETWEEN]
        rig.tick(2)
        self.assertEqual(rig.status()["aircraft"], [])

    def test_the_default_map_range_shows_aircraft_beyond_the_radius(self):
        rig = DaemonRig(self, {**self.SETTINGS, "radius_receiver": "1"})
        rig.aircraft = [self.BETWEEN]
        rig.tick(2)
        status = rig.status()
        self.assertEqual([a["hex"] for a in status["aircraft"]], [self.BETWEEN["hex"]])
        self.assertEqual(status["map_range"], 25.0)
        self.assertEqual(status["aircraft_in_area"], 0)

    def test_online_queries_cover_the_online_radius_even_if_the_receiver_one_is_small(self):
        rig = DaemonRig(self, {**self.SETTINGS, "radius_receiver": "1", "source_mode": "online"})
        calls = []
        real = rig.fetch

        def spy(url):
            calls.append(url)
            return real(url)

        rig.daemon.feed._fetch = spy
        rig.tick(1)
        # Twice the distance to the 13 mi square's corner, in nm, rounded up.
        self.assertTrue(calls[0].endswith("/dist/32"), calls[0])


class MissingRadiusTests(unittest.TestCase):
    INSIDE = {"hex": "a1", "alt_baro": 3000, "gs": 120.0, "seen_pos": 0.5, "lat": 40.05, "lon": -75.0}

    def rig(self, values):
        rig = DaemonRig(self, values, default_radii=False)
        rig.aircraft = [self.INSIDE]
        return rig

    def test_a_missing_radius_stays_idle_with_a_warning(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick(2)
        self.assertEqual((rig.daemon.engine.state, rig.daemon.engine.reason), ("IDLE", "no_zone"))
        self.assertEqual(rig.fpp.ranges, {})
        self.assertTrue(any("zone" in w for w in rig.status()["warnings"]))

    def test_both_mode_needs_both(self):
        rig = self.rig({"source_mode": "both", "receiver_url": RX, "radius": "13", "personal_use_confirmed": "1"})
        rig.tick(2)
        self.assertEqual(rig.daemon.engine.reason, "no_zone")

    def test_entering_it_starts_protection_and_clears_the_warning(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick(2)
        with open(rig.paths.config_file, "a", encoding="utf-8") as handle:
            handle.write('radius_receiver = "10"\n')
        info = os.stat(rig.paths.config_file)
        os.utime(rig.paths.config_file, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        rig.tick(2)
        self.assertEqual((rig.daemon.engine.state, rig.daemon.engine.reason), ("BLACKOUT", "aircraft"))
        self.assertFalse(any("zone" in w for w in rig.status()["warnings"]))

    def test_no_online_requests_without_an_online_radius(self):
        rig = self.rig({"source_mode": "online", "personal_use_confirmed": "1"})
        rig.tick(5)
        self.assertEqual(rig.online_calls, 0)

    def test_status_is_still_usable(self):
        rig = self.rig({"source_mode": "receiver", "receiver_url": RX, "personal_use_confirmed": "1"})
        rig.tick(2)
        status = rig.status()
        self.assertEqual(status["zones"], [])
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
