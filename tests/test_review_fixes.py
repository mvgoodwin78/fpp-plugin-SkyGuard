"""Fixes for gaps found in the whole-plugin review of Nov 8, 2026. Each test here failed before
its fix, so the gap can't come back unnoticed."""

import json
import logging
import os
import threading
import unittest
from unittest import mock

from skyguard.config import ConfigStore, Settings
from skyguard.feed_manager import FEED_ONLINE, FEED_RECEIVER, FeedState
from skyguard.feeds.base import AircraftRecord
from skyguard.fpp_watch import STALE_SHOW_READ_S, FppWatcher
from skyguard.notifier import RepeatLimiter
from skyguard.overlay import Ledger
from skyguard.trigger import data_is_fresh, evaluate_zone
from tests.test_daemon import A_DIMMER, A_SHUTTER, B_DIMMER, PLAYING, Rig, plane
from tests.test_feed_manager import ONLINE, RX, Harness, setUpModule  # noqa: F401 (fast test provider)

try:
    import fcntl
except ImportError:
    fcntl = None


class ResumeBeforeOutputsReadTests(unittest.TestCase):
    """A restart during a blackout, while FPP can't yet say where universe heads are."""

    def test_the_heads_stay_dark_until_the_outputs_can_be_read(self):
        r = Rig(self)
        r.tick()
        r.aircraft = [plane()]
        r.tick()
        self.assertEqual(r.fpp.output(A_DIMMER), 0)
        r.fpp.universes_status = 500
        r.start()  # a crash and restart
        r.tick(0.25, steps=8)
        self.assertEqual(r.state, "BLACKOUT")
        self.assertEqual((r.fpp.output(A_DIMMER), r.fpp.output(A_SHUTTER), r.fpp.output(B_DIMMER)), (0, 8, 0))
        self.assertEqual(r.fpp.ever_full_on, [])

    def test_once_the_outputs_are_read_it_carries_on_normally(self):
        r = Rig(self)
        r.tick()
        r.aircraft = [plane()]
        r.tick()
        r.fpp.universes_status = 500
        r.start()
        r.tick()
        r.fpp.universes_status = 200
        r.aircraft = []
        r.tick(1.0, steps=8)
        self.assertEqual(r.state, "CLEAR")
        self.assertEqual(r.fpp.ranges, {})
        self.assertEqual(r.fpp.ever_full_on, [])

    def test_an_error_during_the_blackout_also_keeps_what_was_held(self):
        r = Rig(self)
        r.tick()
        r.aircraft = [plane()]
        r.tick()
        r.fpp.universes_status = 500
        r.start()
        with mock.patch.object(r.daemon, "_evaluate", side_effect=RuntimeError("surprise")):
            r.tick()
        self.assertEqual(r.fpp.output(A_DIMMER), 0)
        self.assertEqual(r.fpp.output(B_DIMMER), 0)


class TornConfigReadTests(unittest.TestCase):
    """FPP empties the settings file and rewrites it in place."""

    def setUp(self):
        self.r = Rig(self)
        self.r.tick()
        self.r.aircraft = [plane()]
        self.r.tick()
        self.assertEqual(self.r.state, "BLACKOUT")

    def write_raw(self, text):
        with open(self.r.paths.config_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.r._touch(self.r.paths.config_file)

    def test_an_empty_read_mid_write_does_not_release(self):
        self.write_raw("")
        self.r.tick(0.25)
        self.assertEqual(self.r.state, "BLACKOUT")
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)
        self.r.write_settings()  # the write completes
        self.r.tick(0.25, steps=4)
        self.assertEqual(self.r.state, "BLACKOUT")
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_a_half_written_file_is_not_believed(self):
        full = "".join(f'{k} = "{v}"\n' for k, v in self.r.settings_doc.items())
        cut = full.index("personal_use_confirmed")
        self.write_raw(full[: cut + len('personal_use_confirmed = "')])
        self.r.tick(0.25)
        self.assertEqual(self.r.state, "BLACKOUT")
        self.assertEqual(self.r.fpp.output(A_DIMMER), 0)

    def test_a_change_that_stays_is_believed_after_a_moment(self):
        # Something that doesn't lock, or a hand edit, can really drop a key.
        doc = dict(self.r.settings_doc)
        del doc["personal_use_confirmed"]
        self.write_raw("".join(f'{k} = "{v}"\n' for k, v in doc.items()))
        self.r.tick(0.25)
        self.assertEqual(self.r.state, "BLACKOUT")
        self.r.tick(0.5, steps=3)
        self.assertEqual(self.r.reason, "not_confirmed")

    def test_ordinary_saves_apply_at_once(self):
        self.r.write_settings(personal_use_confirmed="0")
        self.r.tick(0.25)
        self.assertEqual(self.r.reason, "not_confirmed")

    @unittest.skipIf(fcntl is None, "needs flock")
    def test_a_file_fpp_is_writing_is_not_read(self):
        with open(self.r.paths.config_file, "r+", encoding="utf-8") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX)
            writer.truncate(0)
            self.r._touch(self.r.paths.config_file)
            self.r.tick(0.25)
            self.assertEqual(self.r.state, "BLACKOUT")
            writer.write("".join(f'{k} = "{v}"\n' for k, v in self.r.settings_doc.items()))
        self.r.tick(0.25)
        self.assertEqual(self.r.state, "BLACKOUT")


class StoreSuspectReadTests(unittest.TestCase):
    def test_the_first_read_of_a_new_install_is_believed(self):
        import tempfile
        from skyguard.config import Paths

        base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))
        paths = Paths.from_env({"MEDIADIR": base})
        os.makedirs(os.path.dirname(paths.config_file))
        with open(paths.config_file, "w", encoding="utf-8") as handle:
            handle.write('enabled = "1"')  # no newline at the end, but nothing was lost
        store = ConfigStore(paths, clock=lambda: 0.0)
        self.assertTrue(store.settings.enabled)


class MigrateTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.path = os.path.join(tempfile.mkdtemp(), "fixtures.json")

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(doc if isinstance(doc, str) else json.dumps(doc))

    def test_rows_without_an_id_get_one_and_others_keep_theirs(self):
        from skyguard.migrate import add_fixture_ids

        self.write([{"name": "A"}, {"name": "B", "id": "keep"}, {"name": "C", "id": ""}])
        self.assertTrue(add_fixture_ids(self.path))
        with open(self.path, encoding="utf-8") as handle:
            rows = json.load(handle)
        self.assertEqual(rows[1]["id"], "keep")
        self.assertTrue(rows[0]["id"] and rows[2]["id"] and rows[0]["id"] != rows[2]["id"])
        self.assertFalse(add_fixture_ids(self.path))  # nothing more to do

    def test_an_unreadable_file_is_left_alone(self):
        from skyguard.migrate import add_fixture_ids

        self.write("{broken")
        self.assertFalse(add_fixture_ids(self.path))
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{broken")


class NegativeAltitudeTests(unittest.TestCase):
    def record(self, alt):
        return AircraftRecord("a1", None, 40.0, -75.0, alt, 140.0, None, 0.5, "receiver")

    def test_an_aircraft_reporting_below_zero_counts_with_the_default_floor(self):
        zone = evaluate_zone([self.record(-130.0)], 40.0, -75.0, 5.0, 0.0, 10000.0, True)
        self.assertEqual(len(zone.in_zone), 1)

    def test_a_negative_floor_is_no_floor_either(self):
        zone = evaluate_zone([self.record(-900.0)], 40.0, -75.0, 5.0, -100.0, 10000.0, True)
        self.assertEqual(len(zone.in_zone), 1)

    def test_a_real_floor_still_applies(self):
        zone = evaluate_zone([self.record(-130.0)], 40.0, -75.0, 5.0, 500.0, 10000.0, True)
        self.assertEqual(zone.in_zone, ())


class LedgerThreadTests(unittest.TestCase):
    def test_two_threads_never_lose_a_held_channel(self):
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "active_ranges.json")
        ledger = Ledger(path)
        errors = []

        def channels():
            try:
                for channel in range(1, 400):
                    ledger.hold_channels({channel: 0})
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        def matrix():
            try:
                for n in range(400):
                    ledger.remember_matrix(f"M{n % 7}", 0)
                    ledger.forget_matrix(f"M{(n + 3) % 7}")
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=channels), threading.Thread(target=matrix)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        ledger.save()
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(len(saved["channels"]), 399)

    def test_saves_never_overlap(self):
        # Two overlapping saves could finish in either order, so an older copy could land last.
        import tempfile
        import time
        from skyguard import overlay

        ledger = Ledger(os.path.join(tempfile.mkdtemp(), "active_ranges.json"))
        writing = []
        overlaps = []
        real_write = overlay.atomic_write_json

        def slow_write(path, doc):
            writing.append(1)
            if len(writing) > 1:
                overlaps.append(len(writing))
            time.sleep(0.002)
            real_write(path, doc)
            writing.pop()

        with mock.patch.object(overlay, "atomic_write_json", slow_write):
            threads = [
                threading.Thread(target=lambda: [ledger.hold_channels({c: 0}) for c in range(1, 60)]),
                threading.Thread(target=lambda: [ledger.remember_matrix(f"M{n}", 0) for n in range(60)]),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(overlaps, [])


class HungPollTests(unittest.TestCase):
    def test_with_max_age_0_a_hung_poll_goes_stale(self):
        self.assertTrue(data_is_fresh(100.0, 99.0, False, 0, poll_window_s=4.0))
        self.assertFalse(data_is_fresh(105.0, 99.0, False, 0, poll_window_s=4.0))

    def test_the_feed_manager_gives_the_window(self):
        h = Harness(source_mode="receiver", max_data_age_s=0)
        h.run_for(1)
        self.assertTrue(h.manager.data_fresh())
        h.net.hook = lambda url: None
        h.time += 10.0  # no poll finished in that time
        self.assertFalse(h.manager.data_fresh())


class FppRestartTests(unittest.TestCase):
    def test_held_ranges_come_back_on_the_next_step(self):
        r = Rig(self)
        r.fpp.status = {**PLAYING, "uptimeTotalSeconds": 5000}
        r.tick()
        r.aircraft = [plane()]
        r.tick()
        r.fpp.restart_fppd()
        r.fpp.status = {**PLAYING, "uptimeTotalSeconds": 2}
        r.tick(0.25)
        self.assertEqual(r.fpp.output(A_DIMMER), 0)
        self.assertEqual(r.fpp.output(B_DIMMER), 0)

    def test_the_outputs_are_read_again_after_a_restart(self):
        r = Rig(self)
        r.fpp.status = {**PLAYING, "uptimeTotalSeconds": 5000}
        r.tick()
        moved = json.loads(json.dumps(r.fpp.universes))
        moved["channelOutputs"][0]["universes"][0]["startChannel"] = 1001
        r.fpp.universes = moved
        r.fpp.status = {**PLAYING, "uptimeTotalSeconds": 3}
        r.aircraft = [plane()]
        r.tick()
        self.assertEqual(r.fpp.output(1001 + 10 - 1), 0)  # head A's dimmer where universe 1 now starts


class WatcherTests(unittest.TestCase):
    class Detector:
        def __init__(self):
            self.answer = False
            self.uptime = None

        def read(self):
            return self.answer

    class Resolver:
        def __init__(self):
            self.forced = []

        def read_outputs(self, force=False):
            self.forced.append(force)

    def setUp(self):
        self.now = 0.0
        self.detector = self.Detector()
        self.resolver = self.Resolver()
        self.watch = FppWatcher(self.detector, self.resolver, lambda: self.now)

    def test_before_any_reading_it_counts_as_a_show(self):
        self.assertTrue(self.watch.in_show(0.0))

    def test_a_fresh_reading_is_used(self):
        self.watch.poll_once()
        self.assertFalse(self.watch.in_show(1.0))

    def test_an_old_reading_counts_as_a_show(self):
        self.watch.poll_once()
        self.assertTrue(self.watch.in_show(STALE_SHOW_READ_S + 0.1))

    def test_uptime_going_down_is_a_restart(self):
        self.detector.uptime = 100
        self.watch.poll_once()
        self.detector.uptime = 101
        self.watch.poll_once()
        self.assertFalse(self.watch.take_restart())
        self.detector.uptime = 1
        self.watch.poll_once()
        self.assertTrue(self.watch.take_restart())
        self.assertFalse(self.watch.take_restart())
        self.assertEqual(self.resolver.forced, [False, False, True])

    def test_a_slow_fpp_does_not_hold_up_the_main_loop(self):
        r = Rig(self)
        r.tick()
        r.aircraft = [plane()]
        with mock.patch.object(r.daemon.api, "get_status", side_effect=AssertionError("blocked on FPP")):
            r.time += 1.0
            r.daemon.feed.poll_once()
            r.daemon.step()  # the step itself never asks FPP for its status
        self.assertEqual(r.state, "BLACKOUT")


class SwitchBackRadiusTests(unittest.TestCase):
    def test_online_aircraft_go_by_the_online_radius_after_the_receiver_takes_over(self):
        r = Rig(self)
        r.write_settings(source_mode="both", radius="13", radius_receiver="5")
        r.tick()
        # About 9 nm out: outside the receiver radius, inside the online one.
        online = AircraftRecord("c3", None, 40.15, -75.0, 3000.0, 140.0, None, 0.5, "online:adsb.fi")
        state = FeedState(FEED_RECEIVER, True, True, (online,), 0, 0.0, False)
        self.assertEqual(len(r.daemon._evaluate(state).in_zone), 1)
        receiver = AircraftRecord("a1", None, 40.15, -75.0, 3000.0, 140.0, None, 0.5, "receiver")
        state = FeedState(FEED_RECEIVER, True, True, (receiver,), 0, 0.0, False)
        self.assertEqual(r.daemon._evaluate(state).in_zone, ())


class BetweenShowsTests(unittest.TestCase):
    def settings(self, **overrides):
        return {"online_between_shows": False, **overrides}

    def test_nothing_is_asked_online_outside_a_show_by_default(self):
        h = Harness(**self.settings(source_mode="online"))
        h.run_for(120)
        self.assertEqual(h.net.count(ONLINE), 0)
        self.assertFalse(h.manager.state().polling)

    def test_a_show_starting_polls_at_once_and_ending_stops(self):
        h = Harness(**self.settings(source_mode="online"))
        h.run_for(5)
        h.manager.set_in_schedule(True)
        h.manager.poll_once()
        self.assertEqual(h.net.count(ONLINE), 1)
        h.run_for(10)
        during = h.net.count(ONLINE)
        self.assertGreater(during, 1)
        h.manager.set_in_schedule(False)
        h.run_for(120)
        self.assertEqual(h.net.count(ONLINE), during)

    def test_turning_it_on_polls_between_shows(self):
        h = Harness(**self.settings(source_mode="online", online_between_shows=True))
        h.run_for(120)
        self.assertEqual(h.net.count(ONLINE), 4)

    def test_a_dead_receiver_does_not_start_online_polling_between_shows(self):
        from skyguard.feeds.base import FeedError

        h = Harness(**self.settings(source_mode="both"))
        h.net.errors[RX] = FeedError("down")
        h.run_for(120)
        self.assertEqual(h.net.count(ONLINE), 0)
        self.assertEqual(h.manager.state().active_feed, FEED_RECEIVER)

    def test_a_dead_receiver_falls_back_once_a_show_starts(self):
        from skyguard.feeds.base import FeedError

        h = Harness(**self.settings(source_mode="both"))
        h.net.errors[RX] = FeedError("down")
        h.run_for(60)
        h.manager.set_in_schedule(True)
        h.run_for(2)
        self.assertEqual(h.manager.state().active_feed, FEED_ONLINE)
        self.assertGreater(h.net.count(ONLINE), 0)


class RepeatLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        self.logger = logging.getLogger("skyguard.test_repeat")
        self.logger.addHandler(handler)
        self.logger.propagate = False
        self.addCleanup(self.logger.removeHandler, handler)
        self.limiter = RepeatLimiter(self.logger, interval_s=60.0, clock=lambda: self.now)

    def fail(self, message="boom"):
        try:
            raise RuntimeError(message)
        except RuntimeError:
            self.limiter.exception("Loop error")

    def test_a_repeating_error_is_logged_in_full_once_then_counted(self):
        for _ in range(240):  # a minute at four a second
            self.fail()
            self.now += 0.25
        self.fail()
        self.assertEqual(len(self.records), 2)
        self.assertIsNotNone(self.records[0].exc_info)
        self.assertIn("240 more times", self.records[1].getMessage())

    def test_a_new_error_is_logged_in_full(self):
        self.fail("one")
        self.fail("two")
        self.assertEqual(len(self.records), 2)
        self.assertIsNotNone(self.records[1].exc_info)

    def test_after_recovery_the_next_error_is_logged_in_full(self):
        self.fail()
        self.limiter.clear()
        self.fail()
        self.assertEqual(len(self.records), 2)


if __name__ == "__main__":
    unittest.main()
