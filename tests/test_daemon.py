"""The whole backend in one process: real FPP HTTP against the fake, a fake clock and feed.

These follow the spec's acceptance list as closely as is possible without hardware.
"""

import json
import os
import shutil
import tempfile
import time
import unittest

from skyguard import commands
from skyguard.config import Paths
from skyguard.daemon import Daemon
from skyguard.feeds.base import FeedError
from skyguard.fpp_api import FppApi
from skyguard.matrix import REFRESH_INTERVAL_S
from skyguard.overlay import HOLD_RESEND_S
from skyguard.status import PERIODIC_WRITE_S
from tests.fake_fpp_server import FakeFpp

RECEIVER = "http://rx/aircraft.json"
ONLINE_PREFIX = "https://opendata.adsb.fi/"
CENTER = {"center_lat": 40.0, "center_lon": -75.0}
INSIDE = {"lat": 40.05, "lon": -75.0}  # about 3 nm from center
OUTSIDE = {"lat": 41.0, "lon": -75.0}  # about 60 nm from center
PLAYING = {"status": 1, "status_name": "playing", "scheduler": {"status": "playing"}}
IDLE_STATUS = {"status": 0, "status_name": "idle", "scheduler": {"status": "idle"}}
UNIVERSES = {
    "channelOutputs": [
        {
            "type": "universes",
            "enabled": 1,
            "universes": [
                {"active": 1, "id": 1, "startChannel": 1, "channelCount": 512},
                {"active": 1, "id": 2, "startChannel": 513, "channelCount": 512},
            ],
        }
    ]
}
HEAD_A = {"name": "A", "universe": 1, "dimmer_ch": 10, "shutter_ch": 11, "shutter_off": 8}
HEAD_B = {"name": "B", "universe": 2, "dimmer_ch": 20}
A_DIMMER, A_SHUTTER, B_DIMMER = 10, 11, 532
PROP = 12  # same universe as head A, not listed


def plane(position=INSIDE, alt=3000, gs=120.0, hex="a1", **extra):
    return {"hex": hex, "alt_baro": alt, "gs": gs, "seen_pos": 0.5, **position, **extra}


class Rig:
    def __init__(self, test, fpp_args=None):
        self.test = test
        self.time = 1000.0
        self.wall = 1793900000.0
        self.dir = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.paths = Paths.from_env({"MEDIADIR": self.dir})
        os.makedirs(os.path.dirname(self.paths.config_file))
        os.makedirs(self.paths.data_dir)
        self.fpp = FakeFpp(**(fpp_args or {}))
        test.addCleanup(self.fpp.stop)
        self.fpp.universes = UNIVERSES
        self.fpp.status = PLAYING
        self.fpp.on_setting = self._fpp_saved_setting
        self.aircraft = []
        self.receiver_down = False
        self.online_down = False
        self.receiver_now = 0.0
        self.online_now = 0.0
        self.online_requests = 0
        self.settings_doc = {
            "source_mode": "receiver",
            "receiver_url": RECEIVER,
            "release_delay_s": "5",
            "personal_use_confirmed": "1",
            "radius": "10",
            "radius_receiver": "10",
        }
        self._tick = 0
        self.write_settings()
        self.write_private(CENTER)
        self.write_fixtures([HEAD_A, HEAD_B])
        self.daemon = None
        self.start()

    def _fpp_saved_setting(self, name, value):
        self.settings_doc[name] = str(value)
        self.write_settings()

    def _touch(self, path):
        info = os.stat(path)
        self._tick += 1
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + self._tick * 1_000_000_000))

    def write_settings(self, **changes):
        self.settings_doc.update({k: str(v) for k, v in changes.items()})
        with open(self.paths.config_file, "w", encoding="utf-8") as handle:
            for key, value in self.settings_doc.items():
                handle.write(f'{key} = "{value}"\n')
        self._touch(self.paths.config_file)

    def write_private(self, doc):
        with open(self.paths.private_file, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        self._touch(self.paths.private_file)

    def write_fixtures(self, rows):
        with open(self.paths.fixtures_file, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        self._touch(self.paths.fixtures_file)

    def fetch(self, url):
        if url.startswith(ONLINE_PREFIX):
            self.online_requests += 1
            if self.online_down:
                raise FeedError("down")
            self.online_now += 1
            return {"ac": self.aircraft, "msg": "No error", "now": self.online_now * 1000}
        if self.receiver_down:
            raise FeedError("down")
        self.receiver_now += 1
        return {"now": self.receiver_now, "aircraft": self.aircraft}

    def start(self):
        """A new daemon process, as after a crash: nothing carries over except the files."""
        self.daemon = Daemon(
            self.paths,
            FppApi(self.fpp.url, timeout_s=1.0),
            fetch=self.fetch,
            clock=lambda: self.time,
            wall_clock=lambda: self.wall,
        )
        return self.daemon

    def tick(self, seconds=1.0, steps=1):
        for _ in range(steps):
            self.time += seconds
            self.wall += seconds
            self.daemon.feed.poll_once()
            self.daemon.feed.health_once()
            self.daemon.feed.poll_second_once()
            self.daemon.step()
            self.daemon.matrix_step()

    @property
    def state(self):
        return self.daemon.engine.state

    @property
    def reason(self):
        return self.daemon.engine.reason

    def status(self):
        with open(self.paths.status_file, encoding="utf-8") as handle:
            return json.load(handle)

    def log_events(self):
        return [event.name for event in self.events]


class BlackoutAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.tick()

    def assert_dark(self):
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertEqual(self.r.fpp.output(A_SHUTTER), 8)
        self.assertEqual(self.r.fpp.output(B_DIMMER), 0)

    def assert_following_sequence(self):
        for channel in (A_DIMMER, A_SHUTTER, B_DIMMER):
            self.assertEqual(self.r.fpp.output(channel), 100)

    def test_aircraft_enters_and_channels_are_forced_on_the_next_poll(self):
        self.assert_following_sequence()
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")
        self.assertEqual(self.r.reason, "aircraft")
        self.assert_dark()

    def test_only_the_listed_channels_change(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual(self.r.fpp.output(PROP), 100)
        self.assertEqual(self.r.fpp.output(A_DIMMER - 1), 100)
        self.assertEqual(self.r.fpp.output(513), 100)
        self.assertEqual(set(self.r.fpp.ranges), {A_DIMMER, A_SHUTTER, B_DIMMER})

    def test_above_ceiling_or_below_floor_does_nothing(self):
        self.r.write_settings(alt_floor_ft=1000)
        self.r.aircraft = [plane(alt=10001), plane(alt=999, hex="a2")]
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.fpp.ranges, {})

    def test_outside_the_radius_does_nothing(self):
        self.r.aircraft = [plane(OUTSIDE)]
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")

    def test_ground_with_floor_zero_blacks_out(self):
        self.r.aircraft = [plane(alt="ground", gs=5.0)]
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")

    def test_no_position_blacks_out_when_the_toggle_is_on(self):
        self.r.aircraft = [{"hex": "a1", "alt_baro": 3000}]
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")

    def test_no_position_is_logged_only_when_the_toggle_is_off(self):
        self.r.write_settings(count_no_position=0)
        self.r.aircraft = [{"hex": "a1", "alt_baro": 3000}]
        with self.assertLogs("skyguard", level="INFO") as captured:
            self.r.tick(steps=3)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(sum("no position" in line for line in captured.output), 1)

    def test_zone_clears_and_channels_release_after_the_delay(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.r.aircraft = []
        self.r.tick(steps=5)
        self.assert_dark()
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")
        self.assert_following_sequence()
        self.assertEqual(self.r.fpp.ranges, {})
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_release_delay_zero_releases_immediately(self):
        self.r.write_settings(release_delay_s=0)
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assert_dark()
        self.r.aircraft = []
        self.r.tick()
        self.assert_following_sequence()

    def test_every_transition_is_logged_with_reason_and_aircraft(self):
        self.r.aircraft = [plane(flight="TEST1 ")]
        with self.assertLogs("skyguard", level="INFO") as started:
            self.r.tick()
        line = next(x for x in started.output if "blackout_started" in x)
        self.assertIn('"reason": "aircraft"', line)
        self.assertIn("TEST1", line)

        self.r.aircraft = []
        with self.assertLogs("skyguard", level="INFO") as cleared:
            self.r.tick(steps=6)
        self.assertTrue(any("blackout_cleared" in x for x in cleared.output))

    def test_a_reason_change_within_blackout_is_logged(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.r.receiver_down = True
        with self.assertLogs("skyguard", level="INFO") as captured:
            self.r.tick(steps=7)
        self.assertTrue(any("reason no_data" in x for x in captured.output))

    def test_settings_apply_without_a_restart(self):
        self.r.aircraft = [plane(alt=12000)]
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")
        self.r.write_settings(alt_ceiling_ft=15000)
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")

    def test_center_change_applies_without_a_restart(self):
        self.r.aircraft = [plane(OUTSIDE)]
        self.r.tick()
        self.r.write_private({"center_lat": 41.0, "center_lon": -75.0})
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")

    def test_fixture_edits_apply_without_a_restart(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.r.write_fixtures([HEAD_A])
        self.r.tick()
        self.assertEqual(self.r.fpp.output(B_DIMMER), 100)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_a_half_filled_fixture_is_ignored(self):
        self.r.write_fixtures([HEAD_A, {"name": "Half", "universe": 1}])
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual(set(self.r.fpp.ranges), {A_DIMMER, A_SHUTTER})

    def test_usb_dmx_head_using_start_channel(self):
        self.r.write_fixtures([{"name": "USB", "start_channel": 5001, "dimmer_ch": 3}])
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual(self.r.fpp.output(5003), 0)

    def test_universe_not_found_warns_in_status(self):
        self.r.write_fixtures([{"name": "Lost", "universe": 99, "dimmer_ch": 1}])
        self.r.tick()
        status = self.r.status()
        self.assertFalse(status["fixtures"][0]["valid"])
        self.assertEqual(status["fixtures"][0]["problem"], "Universe not found")
        self.assertTrue(any("'Lost'" in w for w in status["warnings"]))

    def test_ignore_ground_omits_taxiing_and_counts_takeoff(self):
        self.r.write_settings(ignore_ground=1)
        self.r.aircraft = [plane(alt="ground", gs=15.0)]
        with self.assertLogs("skyguard", level="INFO") as captured:
            self.r.tick(steps=3)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(sum("ground_aircraft_ignored" in line for line in captured.output), 1)
        self.r.tick(PERIODIC_WRITE_S)
        aircraft = self.r.status()["aircraft"][0]
        self.assertEqual(aircraft["note"], "ground (ignored)")
        self.r.aircraft = [plane(alt="ground", gs=45.0)]
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")


class FeedFailureAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.tick()

    def test_feed_silent_blacks_out_with_no_data_and_recovers(self):
        self.r.receiver_down = True
        self.r.tick(steps=5)
        self.assertEqual(self.r.state, "CLEAR")
        self.r.tick()
        self.assertEqual((self.r.state, self.r.reason), ("BLACKOUT", "no_data"))
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.r.receiver_down = False
        self.r.tick(steps=6)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.fpp.output(A_DIMMER), 100)

    def test_max_data_age_zero_blacks_out_on_the_first_failed_poll(self):
        self.r.write_settings(max_data_age_s=0)
        self.r.tick()
        self.r.receiver_down = True
        self.r.tick()
        self.assertEqual(self.r.reason, "no_data")

    def test_receiver_stops_online_takes_over_and_receiver_returns(self):
        self.r.write_settings(source_mode="both")
        self.r.tick()
        self.r.receiver_down = True
        self.r.tick(steps=25)
        status = self.r.status()
        self.assertEqual(status["active_feed"], "online")
        self.assertFalse(status["receiver_ok"])
        self.assertTrue(any("online feed" in w for w in status["warnings"]))
        self.assertGreater(self.r.online_requests, 0)
        self.r.receiver_down = False
        self.r.tick(steps=12)
        status = self.r.status()
        self.assertEqual(status["active_feed"], "receiver")
        self.assertEqual(status["warnings"], [])

    def test_both_feeds_down_holds_the_blackout_with_a_warning(self):
        self.r.write_settings(source_mode="both")
        self.r.tick()
        self.r.receiver_down = True
        self.r.online_down = True
        self.r.tick(steps=40)
        self.assertEqual((self.r.state, self.r.reason), ("BLACKOUT", "no_data"))
        self.assertTrue(any("No aircraft data" in w for w in self.r.status()["warnings"]))
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)


class ScheduleAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)

    def test_outside_the_schedule_nothing_is_overridden(self):
        self.r.fpp.status = IDLE_STATUS
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual((self.r.state, self.r.reason), ("IDLE", "schedule"))
        self.assertEqual(self.r.fpp.ranges, {})

    def test_schedule_starts_with_an_aircraft_present(self):
        self.r.fpp.status = IDLE_STATUS
        self.r.aircraft = [plane()]
        self.r.tick()
        self.r.fpp.status = PLAYING
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)

    def test_schedule_end_releases_at_once(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.r.fpp.status = IDLE_STATUS
        self.r.tick()
        self.assertEqual(self.r.state, "IDLE")
        self.assertEqual(self.r.fpp.ranges, {})

    def test_fpp_status_unreachable_counts_as_a_show(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.r.fpp.status = IDLE_STATUS
        self.r.fpp.available = False
        self.r.tick()
        self.assertTrue(self.r.status()["in_schedule"])

    def test_show_start_polls_an_online_feed_at_once(self):
        self.r.write_settings(source_mode="online")
        self.r.fpp.status = IDLE_STATUS
        self.r.tick(steps=3)
        before = self.r.online_requests
        self.r.fpp.status = PLAYING
        self.r.tick()  # the daemon sees the show start
        self.r.tick()  # the next poll doesn't wait for the 30 s interval
        self.assertEqual(self.r.online_requests, before + 1)

    def test_center_not_set_stays_idle_with_a_warning(self):
        self.r.write_private({})
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual((self.r.state, self.r.reason), ("IDLE", "no_center"))
        self.assertTrue(any("center point" in w for w in self.r.status()["warnings"]))


class HeadTestTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.write_fixtures([{**HEAD_A, "id": "aaa"}, {**HEAD_B, "id": "bbb"}])
        self.r.tick()

    def send(self, seconds, fixture):
        commands.send_command(self.r.paths, commands.TEST, seconds, fixture)
        self.r.wall = time.time()

    def test_only_that_head_goes_dark_for_the_time_given(self):
        self.send(3, "aaa")
        self.r.tick()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertEqual(self.r.fpp.output(A_SHUTTER), 8)
        self.assertEqual(self.r.fpp.output(B_DIMMER), 100)
        self.assertEqual(self.r.state, "CLEAR")
        self.r.tick(steps=2)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.r.tick()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 100)
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_it_works_outside_a_show(self):
        self.r.fpp.status = IDLE_STATUS
        self.r.tick()
        self.send(3, "bbb")
        self.r.tick()
        self.assertEqual(self.r.state, "IDLE")
        self.assertEqual(self.r.fpp.output(B_DIMMER), 0)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 100)

    def test_an_unknown_fixture_does_nothing(self):
        self.send(3, "nope")
        self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})

    def test_it_does_not_cut_a_real_blackout_short(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.send(1, "aaa")
        self.r.tick(steps=2)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertEqual(self.r.fpp.output(B_DIMMER), 0)

    def test_it_waits_for_enable_and_confirmation(self):
        self.r.write_settings(enabled=0)
        self.r.tick()
        self.send(3, "aaa")
        self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})
        self.r.write_settings(enabled=1, personal_use_confirmed=0)
        self.r.tick()
        self.send(3, "aaa")
        self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})

    def test_a_request_made_while_disabled_does_not_fire_after_enabling(self):
        self.r.write_settings(enabled=0)
        self.r.tick()
        self.send(30, "aaa")
        self.r.tick()
        self.r.write_settings(enabled=1)
        self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})

    def test_disabling_during_it_releases(self):
        self.send(30, "aaa")
        self.r.tick()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.r.write_settings(enabled=0)
        self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})

    def test_status_carries_the_fixture_id(self):
        self.r.tick()
        self.assertEqual(self.r.status()["fixtures"][0]["id"], "aaa")

    def test_a_fixture_change_reaches_the_status_at_once(self):
        self.r.write_fixtures([{**HEAD_A, "id": "aaa"}, {"id": "ccc", "name": "Half"}])
        self.r.tick()
        self.assertEqual(self.r.status()["fixtures"][1]["problem"], "Incomplete")


class MessageTestTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.fpp.add_model("Panel")
        self.r.write_settings(
            matrix_enabled=0,
            matrix_models="Panel",
            matrix_position="Center",
            matrix_message="{callsign} {count}",
        )
        self.r.tick()

    def panel(self):
        return self.r.fpp.models["Panel"]

    def send(self, seconds=5):
        commands.send_command(self.r.paths, commands.TEST_MESSAGE, seconds)
        self.r.wall = time.time()

    def test_it_shows_sample_values_then_clears_and_restores_the_panel(self):
        self.send()
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertEqual(self.panel()["effect"], "N123AB 2")
        self.assertEqual(self.panel()["state"], 1)
        self.r.tick(REFRESH_INTERVAL_S, steps=3)
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.panel()["state"], 0)
        self.assertEqual(self.r.daemon.ledger.matrix, {})

    def test_the_heads_are_not_touched(self):
        self.send()
        self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})
        self.assertEqual(self.r.state, "CLEAR")

    def test_it_works_while_disabled(self):
        self.r.write_settings(enabled=0)
        self.r.tick()
        self.send()
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertEqual(self.panel()["effect"], "N123AB 2")

    def test_it_needs_the_confirmation(self):
        self.r.write_settings(personal_use_confirmed=0)
        self.r.tick()
        self.send()
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertIsNone(self.panel()["effect"])

    def test_a_request_made_before_confirming_does_not_fire_after(self):
        self.r.write_settings(personal_use_confirmed=0)
        self.r.tick()
        self.send(60)
        self.r.tick(REFRESH_INTERVAL_S)
        self.r.write_settings(personal_use_confirmed=1)
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertIsNone(self.panel()["effect"])

    def test_withdrawing_the_confirmation_ends_it(self):
        self.send(60)
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertEqual(self.panel()["effect"], "N123AB 2")
        self.r.write_settings(personal_use_confirmed=0)
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertIsNone(self.panel()["effect"])

    def test_a_real_message_wins(self):
        self.r.write_settings(matrix_enabled=1)
        self.r.tick()
        self.send(60)
        self.r.aircraft = [plane(flight="REAL1 ")]
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertEqual(self.panel()["effect"], "REAL1 1")

    def test_no_panels_means_nothing_is_sent(self):
        self.r.write_settings(matrix_models="")
        self.r.tick()
        self.send()
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertEqual(self.r.fpp.commands, [])

    def test_an_old_request_does_not_fire(self):
        commands.send_command(self.r.paths, commands.TEST_MESSAGE, 5)
        self.r.wall = time.time() + 120
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertIsNone(self.panel()["effect"])


class NoFixturesWarningTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)

    def warned(self):
        return any("No moving heads" in w for w in self.r.status()["warnings"])

    def test_no_warning_with_a_working_fixture(self):
        self.r.tick()
        self.assertFalse(self.warned())

    def test_an_empty_list_warns(self):
        self.r.write_fixtures([])
        self.r.tick()
        self.assertTrue(self.warned())

    def test_only_incomplete_or_unresolved_rows_warn(self):
        self.r.write_fixtures([{"name": "Half", "universe": 1}, {"name": "Lost", "universe": 99, "dimmer_ch": 1}])
        self.r.tick()
        self.assertTrue(self.warned())

    def test_only_disabled_rows_warn(self):
        self.r.write_fixtures([{**HEAD_A, "enabled": False}])
        self.r.tick()
        self.assertTrue(self.warned())

    def test_adding_one_clears_it(self):
        self.r.write_fixtures([])
        self.r.tick()
        self.r.write_fixtures([HEAD_A])
        self.r.tick()
        self.assertFalse(self.warned())

    def test_it_stays_quiet_while_disabled_or_unconfirmed(self):
        self.r.write_fixtures([])
        self.r.write_settings(enabled=0)
        self.r.tick()
        self.assertFalse(self.warned())
        self.r.write_settings(enabled=1, personal_use_confirmed=0)
        self.r.tick()
        self.assertFalse(self.warned())


class CommandAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.tick()

    def send(self, name, seconds=None):
        commands.send_command(self.r.paths, name, seconds)
        # send_command stamps real time; align the fake wall clock with it.
        self.r.wall = time.time()

    def test_disable_releases_goes_idle_warns_and_persists(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.send(commands.DISABLE)
        self.r.tick()
        self.assertEqual((self.r.state, self.r.reason), ("IDLE", "disabled"))
        self.assertEqual(self.r.fpp.ranges, {})
        self.assertTrue(any("disabled" in w for w in self.r.status()["warnings"]))
        self.assertEqual(self.r.settings_doc["enabled"], "0")

    def test_enable_resumes_and_clears_the_warning(self):
        self.send(commands.DISABLE)
        self.r.tick()
        self.send(commands.ENABLE)
        self.r.tick(steps=2)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.status()["warnings"], [])
        self.assertEqual(self.r.settings_doc["enabled"], "1")

    def test_disabled_survives_a_restart(self):
        self.send(commands.DISABLE)
        self.r.tick(steps=2)
        self.r.start()
        self.r.tick()
        self.assertEqual((self.r.state, self.r.reason), ("IDLE", "disabled"))

    def test_commands_apply_on_the_same_step(self):
        self.r.aircraft = [plane()]
        self.r.tick()
        self.send(commands.DISABLE)
        self.r.daemon.step()
        self.assertEqual(self.r.state, "IDLE")
        self.assertEqual(self.r.fpp.ranges, {})

    def test_disable_still_applies_if_fpp_cannot_save_it(self):
        self.r.fpp.fail_settings = True
        self.send(commands.DISABLE)
        self.r.tick()
        self.assertEqual(self.r.state, "IDLE")
        self.r.fpp.fail_settings = False
        self.r.tick(steps=6)
        self.assertEqual(self.r.settings_doc["enabled"], "0")
        self.assertEqual(self.r.state, "IDLE")

    def test_online_stops_after_disable(self):
        self.r.write_settings(source_mode="online")
        self.r.tick()
        self.send(commands.DISABLE)
        self.r.tick()
        before = self.r.online_requests
        self.r.tick(steps=60)
        self.assertEqual(self.r.online_requests, before)

    def test_test_trigger_forces_blackout_then_resumes(self):
        self.send(commands.TEST, 10)
        self.r.tick()
        self.assertEqual((self.r.state, self.r.reason), ("BLACKOUT", "test"))
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.r.tick(steps=9)
        self.assertEqual(self.r.reason, "test")
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.fpp.ranges, {})

    def test_test_trigger_default_length(self):
        self.send(commands.TEST)
        self.r.tick(steps=10)
        self.assertEqual(self.r.reason, "test")
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")

    def test_a_test_requested_long_ago_does_not_fire(self):
        self.send(commands.TEST, 10)
        self.r.wall += commands.TEST_EXPIRY_S + 1
        self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")

    def test_command_files_are_consumed(self):
        self.send(commands.TEST, 5)
        self.r.tick()
        self.assertEqual(os.listdir(self.r.paths.commands_dir), [])

    def test_a_garbage_command_file_is_removed_and_ignored(self):
        os.makedirs(self.r.paths.commands_dir, exist_ok=True)
        path = os.path.join(self.r.paths.commands_dir, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{nope")
        with self.assertLogs("skyguard", level="WARNING"):
            self.r.tick()
        self.assertEqual(os.listdir(self.r.paths.commands_dir), [])
        self.assertEqual(self.r.state, "CLEAR")

    def test_an_unknown_command_name_does_nothing(self):
        os.makedirs(self.r.paths.commands_dir, exist_ok=True)
        path = os.path.join(self.r.paths.commands_dir, "odd.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"command": "reboot", "at": self.r.wall}, handle)
        with self.assertLogs("skyguard", level="WARNING"):
            self.r.tick()
        self.assertEqual(self.r.state, "CLEAR")
        self.assertTrue(self.r.status()["enabled"])
        self.assertEqual(os.listdir(self.r.paths.commands_dir), [])

    def test_unknown_commands_cannot_be_queued(self):
        with self.assertRaises(ValueError):
            commands.send_command(self.r.paths, "reboot")

    def test_test_seconds_validation(self):
        self.assertEqual(commands.test_seconds(30), 30)
        self.assertEqual(commands.test_seconds("30"), 30)
        for bad in (None, "abc", 0, -5, 999999, float("inf")):
            self.assertEqual(commands.test_seconds(bad), commands.DEFAULT_TEST_SECONDS)


class CrashAcceptanceTests(unittest.TestCase):
    """A new Daemon with no shutdown call is what a crash and supervisor restart look like."""

    def setUp(self):
        self.r = Rig(self)
        self.r.tick()
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")

    def crash_and_restart(self):
        return self.r.start()

    def test_heads_stay_dark_through_a_crash(self):
        self.crash_and_restart()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)

    def test_restart_resumes_in_blackout(self):
        self.crash_and_restart()
        self.assertEqual((self.r.state, self.r.reason), ("BLACKOUT", "resume"))

    def test_resume_releases_only_once_the_normal_rules_are_met(self):
        self.r.aircraft = []
        self.crash_and_restart()
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.r.tick(steps=5)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.fpp.output(A_DIMMER), 100)
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_aircraft_still_present_keeps_the_blackout(self):
        self.crash_and_restart()
        self.r.tick(steps=10)
        self.assertEqual((self.r.state, self.r.reason), ("BLACKOUT", "aircraft"))

    def test_outside_the_schedule_a_restart_releases_at_once(self):
        self.r.fpp.status = IDLE_STATUS
        self.crash_and_restart()
        self.r.tick()
        self.assertEqual(self.r.state, "IDLE")
        self.assertEqual(self.r.fpp.ranges, {})

    def test_fixture_removed_while_down_is_still_released(self):
        self.r.write_fixtures([HEAD_A])
        self.crash_and_restart()
        self.r.tick()
        self.assertEqual(self.r.fpp.output(B_DIMMER), 100)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_fppd_restart_mid_blackout_restores_the_ranges(self):
        self.r.fpp.restart_fppd()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 100)
        self.r.tick(HOLD_RESEND_S)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)

    def test_fppd_and_daemon_restart_together_never_goes_full_on(self):
        self.r.fpp.restart_fppd()
        self.r.aircraft = []
        self.crash_and_restart()
        self.r.tick(steps=7)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.fpp.ever_full_on, [])
        self.assertEqual(self.r.fpp.ranges, {})

    def test_a_corrupt_ledger_resumes_dark_and_releases_current_fixtures(self):
        with open(self.r.paths.ledger_file, "w", encoding="utf-8") as handle:
            handle.write("{nope")
        with self.assertLogs("skyguard", level="WARNING"):
            self.crash_and_restart()
            self.r.aircraft = []
            self.r.tick(steps=7)
        self.assertEqual(self.r.state, "CLEAR")
        self.assertEqual(self.r.fpp.ranges, {})
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_a_corrupt_ledger_outside_a_show_still_releases_held_channels(self):
        # Going straight to IDLE must not strand ranges nothing remembers holding.
        with open(self.r.paths.ledger_file, "w", encoding="utf-8") as handle:
            handle.write("{nope")
        self.r.fpp.status = IDLE_STATUS
        with self.assertLogs("skyguard", level="WARNING"):
            self.crash_and_restart()
            self.r.tick()
        self.assertEqual(self.r.state, "IDLE")
        self.assertEqual(self.r.fpp.ranges, {})
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_a_clean_shutdown_releases_everything(self):
        self.r.daemon.shutdown()
        self.assertEqual(self.r.fpp.ranges, {})
        self.assertEqual(self.r.fpp.ever_full_on, [])
        self.r.start()
        self.assertEqual(self.r.state, "IDLE")

    def test_a_clean_shutdown_releases_before_waiting_on_feeds(self):
        order = []
        real_release = self.r.daemon.overlay.release_all
        real_stop = self.r.daemon.feed.stop
        self.r.daemon.overlay.release_all = lambda: (order.append("release"), real_release())[1]
        self.r.daemon.feed.stop = lambda **kw: (order.append("feed"), real_stop(**kw))[1]
        self.r.daemon.shutdown()
        self.assertEqual(order, ["release", "feed"])

    def test_a_clean_stop_after_an_fppd_restart_never_goes_full_on(self):
        self.r.fpp.restart_fppd()
        self.r.daemon.shutdown()
        self.assertEqual(self.r.fpp.ever_full_on, [])


class SupervisorWatchTests(unittest.TestCase):
    def rig(self, supervisor_pid, parent):
        rig = Rig(self)
        rig.daemon._supervisor_pid = supervisor_pid
        rig.daemon._parent_pid = lambda: parent["pid"]
        return rig

    def warned(self, rig):
        return any("supervisor" in w for w in rig.daemon.warning_sink.warnings())

    def test_no_warning_while_the_supervisor_is_the_parent(self):
        rig = self.rig(100, {"pid": 100})
        rig.tick(3)
        self.assertFalse(self.warned(rig))

    def test_a_changed_parent_raises_the_warning_once(self):
        parent = {"pid": 100}
        rig = self.rig(100, parent)
        rig.tick()
        parent["pid"] = 1
        with self.assertLogs("skyguard", level="WARNING") as captured:
            rig.tick(5)
        self.assertTrue(self.warned(rig))
        self.assertEqual(sum("supervisor_lost" in line for line in captured.output), 1)

    def test_the_warning_reaches_the_status_file(self):
        rig = self.rig(100, {"pid": 1})
        rig.tick()
        self.assertTrue(any("supervisor" in w for w in rig.status()["warnings"]))

    def test_protection_continues(self):
        rig = self.rig(100, {"pid": 1})
        rig.tick()
        rig.aircraft = [plane()]
        rig.tick()
        self.assertEqual(rig.state, "BLACKOUT")
        self.assertEqual(rig.fpp.output(A_DIMMER), 0)

    def test_a_daemon_not_started_by_a_supervisor_never_warns(self):
        rig = self.rig(None, {"pid": 1})
        rig.tick(2)
        self.assertFalse(self.warned(rig))


class InternalErrorTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.tick()

    def break_evaluation(self):
        def boom(*_args):
            raise RuntimeError("surprise")

        self.original = self.r.daemon._evaluate
        self.r.daemon._evaluate = boom

    def test_during_a_show_an_internal_error_fails_dark(self):
        self.break_evaluation()
        with self.assertLogs("skyguard", level="ERROR"):
            self.r.tick()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertTrue(self.r.daemon.warning_sink.warnings())

    def test_outside_a_show_an_internal_error_stays_released(self):
        self.r.fpp.status = IDLE_STATUS
        self.r.tick()
        self.break_evaluation()
        with self.assertLogs("skyguard", level="ERROR"):
            self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})

    def test_while_disabled_an_internal_error_stays_released(self):
        self.r.write_settings(enabled=0)
        self.r.tick()
        self.break_evaluation()
        with self.assertLogs("skyguard", level="ERROR"):
            self.r.tick()
        self.assertEqual(self.r.fpp.ranges, {})

    def test_recovering_clears_the_warning_and_releases(self):
        self.break_evaluation()
        with self.assertLogs("skyguard", level="ERROR"):
            self.r.tick()
        self.r.daemon._evaluate = self.original
        self.r.tick()
        self.assertEqual(self.r.daemon.warning_sink.warnings(), [])
        self.assertEqual(self.r.fpp.output(A_DIMMER), 100)

    def test_step_never_raises(self):
        self.break_evaluation()
        self.r.daemon.overlay.sync = self.fail
        with self.assertLogs("skyguard", level="ERROR"):
            self.r.daemon.step()


class MatrixAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.fpp.add_model("Panel")
        self.r.write_settings(
            matrix_enabled=1,
            matrix_models="Panel",
            matrix_position="Center",
            matrix_message="{callsign} {count}",
            matrix_nodata_message="Offline",
        )
        self.r.tick()

    def panel(self):
        return self.r.fpp.models["Panel"]

    def test_message_appears_with_placeholders_filled_and_clears_on_release(self):
        self.r.aircraft = [plane(flight="TEST1 ")]
        self.r.tick()
        self.assertEqual(self.panel()["effect"], "TEST1 1")
        self.r.aircraft = []
        self.r.tick(steps=6)
        self.assertEqual(self.r.state, "CLEAR")
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.panel()["state"], 0)

    def test_message_updates_as_values_change(self):
        self.r.aircraft = [plane(flight="TEST1 ")]
        self.r.tick()
        self.r.aircraft = [plane(flight="TEST1 "), plane(hex="a2")]
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertEqual(self.panel()["effect"], "TEST1 2")

    def test_no_data_uses_its_message(self):
        self.r.receiver_down = True
        self.r.tick(steps=7)
        self.assertEqual(self.panel()["effect"], "Offline")

    def test_test_trigger_shows_the_message(self):
        commands.send_command(self.r.paths, commands.TEST, 5)
        self.r.wall = time.time()
        self.r.tick()
        self.assertIn("--", self.panel()["effect"])

    def test_a_crash_with_the_message_up_still_clears_it_later(self):
        self.r.aircraft = [plane(flight="TEST1 ")]
        self.r.tick()
        self.r.start()
        self.r.aircraft = []
        self.r.tick(steps=7)
        self.r.tick(REFRESH_INTERVAL_S)
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.panel()["state"], 0)

    def test_a_matrix_failure_does_not_affect_the_blackout(self):
        self.r.fpp.fail_commands = True
        self.r.aircraft = [plane()]
        with self.assertLogs("skyguard", level="WARNING"):
            self.r.tick()
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)

    def test_blackout_is_applied_before_the_matrix_runs(self):
        self.r.aircraft = [plane()]
        self.r.time += 1
        self.r.daemon.feed.poll_once()
        self.r.daemon.step()  # no matrix_step: that is on its own thread
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.assertIsNone(self.panel()["effect"])

    def test_a_clean_shutdown_clears_the_message(self):
        self.r.aircraft = [plane(flight="TEST1 ")]
        self.r.tick()
        self.r.daemon.shutdown()
        self.assertIsNone(self.panel()["effect"])


class StatusAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.aircraft = [plane(flight="TEST1 "), plane(OUTSIDE, hex="far")]
        self.r.write_private({**CENTER, "online_api_key": "hunter2"})
        self.r.tick()

    def test_all_documented_fields_are_present(self):
        status = self.r.status()
        for field in (
            "state",
            "reason",
            "enabled",
            "in_schedule",
            "active_feed",
            "receiver_ok",
            "last_data_age_s",
            "aircraft_in_zone",
            "center",
            "radius",
            "aircraft",
            "fixtures",
            "warnings",
            "updated_at",
        ):
            self.assertIn(field, status, field)

    def test_values(self):
        status = self.r.status()
        self.assertEqual(status["state"], "BLACKOUT")
        self.assertEqual(status["reason"], "aircraft")
        self.assertTrue(status["enabled"])
        self.assertTrue(status["in_schedule"])
        self.assertEqual(status["active_feed"], "receiver")
        self.assertEqual(status["aircraft_in_zone"], 1)
        self.assertEqual(status["center"], {"lat": 40.0, "lon": -75.0})
        self.assertEqual(status["radius"], {"value": 10.0, "unit": "mi", "online": 10.0, "receiver": 10.0})
        self.assertEqual(status["updated_at"], self.r.wall)

    def test_aircraft_entries(self):
        aircraft = self.r.status()["aircraft"][0]
        self.assertEqual(aircraft["hex"], "a1")
        self.assertEqual(aircraft["callsign"], "TEST1")
        self.assertTrue(aircraft["in_zone"])
        self.assertAlmostEqual(aircraft["distance"], 3.45, delta=0.1)
        self.assertFalse(aircraft["ground_ignored"])
        self.assertIsNone(aircraft["note"])

    def test_aircraft_beyond_the_display_range_are_left_out(self):
        self.assertEqual([a["hex"] for a in self.r.status()["aircraft"]], ["a1"])

    def test_aircraft_between_radius_and_display_range_are_listed_but_not_in_zone(self):
        self.r.aircraft = [plane({"lat": 40.2, "lon": -75.0})]  # about 13.8 mi; radius is 10
        self.r.tick()
        status = self.r.status()
        self.assertEqual(len(status["aircraft"]), 1)
        self.assertFalse(status["aircraft"][0]["in_zone"])

    def test_no_position_aircraft_are_listed(self):
        self.r.aircraft = [{"hex": "np", "alt_baro": 3000}]
        self.r.tick(PERIODIC_WRITE_S)
        entry = self.r.status()["aircraft"][0]
        self.assertIsNone(entry["distance"])
        self.assertTrue(entry["in_zone"])

    def test_fixtures_show_resolved_channels(self):
        fixture = self.r.status()["fixtures"][0]
        self.assertEqual(fixture["name"], "A")
        self.assertTrue(fixture["valid"])
        self.assertEqual(fixture["channels"][0], {"channel": 10, "value": 0, "role": "dimmer"})

    def test_the_api_key_is_never_in_the_status_file(self):
        with open(self.r.paths.status_file, encoding="utf-8") as handle:
            self.assertNotIn("hunter2", handle.read())

    def test_center_not_set_gives_a_usable_status(self):
        self.r.write_private({})
        self.r.tick()
        status = self.r.status()
        self.assertEqual(status["aircraft"], [])
        self.assertEqual(status["aircraft_in_zone"], 0)

    def test_data_age_is_reported(self):
        self.r.receiver_down = True
        self.r.tick(steps=3)
        self.r.tick(PERIODIC_WRITE_S)
        self.assertGreater(self.r.status()["last_data_age_s"], 3)

    def test_routine_writes_are_limited_to_spare_the_sd_card(self):
        before = os.stat(self.r.paths.status_file).st_mtime_ns
        self.r.tick(steps=1)
        self.assertEqual(os.stat(self.r.paths.status_file).st_mtime_ns, before)

    def test_routine_writes_still_happen_periodically(self):
        first = self.r.status()["updated_at"]
        self.r.tick(PERIODIC_WRITE_S)
        self.assertGreater(self.r.status()["updated_at"], first)

    def test_important_changes_are_written_at_once(self):
        self.r.aircraft = []
        self.r.fpp.status = IDLE_STATUS
        self.r.tick()
        self.assertEqual(self.r.status()["state"], "IDLE")

    def test_a_status_write_failure_does_not_stop_the_blackout(self):
        self.r.daemon.status_writer._path = os.path.join(self.r.dir, "config", "plugin.fpp-plugin-SkyGuard", "x")
        with self.assertLogs("skyguard", level="ERROR"):
            self.r.tick(PERIODIC_WRITE_S)
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)


class PrivacyTests(unittest.TestCase):
    def test_the_log_has_no_key_coordinates_or_urls(self):
        r = Rig(self)
        r.write_private({"center_lat": 12.3456, "center_lon": -65.4321, "online_api_key": "hunter2"})
        r.write_settings(source_mode="online")
        r.online_down = True
        with self.assertLogs("skyguard", level="INFO") as captured:
            r.tick(steps=10)
            r.aircraft = [plane({"lat": 12.35, "lon": -65.43})]
            r.online_down = False
            r.tick(steps=3)
        text = "\n".join(captured.output)
        for private in ("hunter2", "12.3456", "65.4321", "opendata.adsb.fi"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
