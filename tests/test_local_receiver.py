"""A receiver on the FPP box itself: SkyGuard reads the decoder's own aircraft.json files."""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from skyguard.config import Settings, load_settings
from skyguard.feed_manager import FEED_RECEIVER, FeedManager
from skyguard.feeds import receiver
from skyguard.feeds.base import FeedError
from skyguard.feeds.receiver import (
    LOCAL_1090_FILES,
    LOCAL_978_FILES,
    LOCAL_978_MAX_LAG_S,
    LOCAL_RECEIVER,
    LocalReceiverAdapter,
    read_local_file,
)
from skyguard.notifier import Notifier, WarningSink

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANE = {"hex": "a1", "alt_baro": 3000, "lat": 40.0, "lon": -75.0, "seen_pos": 0.5}
UAT = {"hex": "b2", "alt_baro": 2000, "lat": 40.1, "lon": -75.1, "seen_pos": 0.5}


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, doc):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(doc if isinstance(doc, str) else json.dumps(doc))
        return self.path(name)

    def adapter(self, n1090=("a", "b"), n978=("u", "v")):
        return LocalReceiverAdapter(
            files_1090=tuple(self.path(n) for n in n1090),
            files_978=tuple(self.path(n) for n in n978),
        )


class SettingTests(unittest.TestCase):
    def test_on_this_box_reads_the_local_files_and_no_address(self):
        settings = load_settings({
            "receiver_location": "this_box",
            "receiver_url": "http://10.0.0.5/data/aircraft.json",
            "receiver_978_url": "http://10.0.0.5/978/aircraft.json",
        })
        self.assertEqual(settings.receiver_url, LOCAL_RECEIVER)
        self.assertEqual(settings.receiver_978_url, "")
        self.assertTrue(settings.uses_receiver)

    def test_another_device_keeps_the_addresses(self):
        settings = load_settings({"receiver_url": "http://10.0.0.5/data/aircraft.json"})
        self.assertEqual(settings.receiver_location, "other")
        self.assertEqual(settings.receiver_url, "http://10.0.0.5/data/aircraft.json")

    def test_an_unknown_location_falls_back_with_a_warning(self):
        settings = load_settings({"receiver_location": "/etc/passwd"})
        self.assertEqual(settings.receiver_location, "other")
        self.assertTrue(any("receiver_location" in w for w in settings.warnings))

    def test_the_local_marker_cant_be_typed_in_as_an_address(self):
        settings = load_settings({"receiver_url": LOCAL_RECEIVER})
        self.assertEqual(settings.receiver_url, "")

    def test_the_settings_page_default_matches(self):
        self.assertIn("'receiver_location', 'other'", read("content.php"))
        self.assertEqual(Settings().receiver_location, "other")


class ReadFileTests(Files):
    def test_a_missing_file_is_none(self):
        self.assertIsNone(read_local_file(self.path("nope")))

    def test_reads_json(self):
        self.assertEqual(read_local_file(self.write("a", {"now": 1, "aircraft": []})), {"now": 1, "aircraft": []})

    def test_bad_json_fails(self):
        with self.assertRaises(FeedError):
            read_local_file(self.write("a", "{half"))

    def test_too_large_fails(self):
        with mock.patch.object(receiver, "MAX_RESPONSE_BYTES", 10):
            with self.assertRaises(FeedError):
                read_local_file(self.write("a", {"now": 1, "aircraft": [PLANE]}))

    def test_a_directory_fails(self):
        os.mkdir(self.path("d"))
        with self.assertRaises(FeedError):
            read_local_file(self.path("d"))

    @unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows")
    def test_a_symlink_is_not_followed(self):
        # The daemon is root: a link planted by the decoder's user mustn't open other files.
        target = self.write("secret", {"now": 1, "aircraft": []})
        os.symlink(target, self.path("a"))
        with self.assertRaises(FeedError):
            read_local_file(self.path("a"))


class AdapterTests(Files):
    def test_nothing_there_fails_dark(self):
        with self.assertRaisesRegex(FeedError, "no decoder found"):
            self.adapter().fetch()

    def test_reads_the_1090_file(self):
        self.write("a", {"now": 100.0, "aircraft": [PLANE]})
        snapshot = self.adapter().fetch()
        self.assertEqual([r.hex for r in snapshot.aircraft], ["a1"])
        self.assertEqual(snapshot.now_s, 100.0)

    def test_the_newest_of_two_decoders_wins(self):
        self.write("a", {"now": 100.0, "aircraft": []})  # left over from an old install
        self.write("b", {"now": 200.0, "aircraft": [PLANE]})
        self.assertEqual(self.adapter().fetch().now_s, 200.0)

    def test_a_bad_leftover_file_doesnt_hide_a_good_one(self):
        self.write("a", "{broken")
        self.write("b", {"now": 200.0, "aircraft": [PLANE]})
        self.assertEqual(self.adapter().fetch().now_s, 200.0)

    def test_only_bad_files_fail(self):
        self.write("a", "{broken")
        with self.assertRaises(FeedError):
            self.adapter().fetch()

    def test_978_is_merged_in(self):
        self.write("a", {"now": 100.0, "aircraft": [PLANE]})
        self.write("u", {"now": 99.0, "aircraft": [UAT]})
        snapshot = self.adapter().fetch()
        self.assertEqual(sorted(r.hex for r in snapshot.aircraft), ["a1", "b2"])
        self.assertEqual(snapshot.now_s, 100.0)

    def test_a_stopped_978_decoder_is_left_out(self):
        self.write("a", {"now": 100.0, "aircraft": [PLANE]})
        self.write("u", {"now": 100.0 - LOCAL_978_MAX_LAG_S - 1, "aircraft": [UAT]})
        self.assertEqual([r.hex for r in self.adapter().fetch().aircraft], ["a1"])

    def test_a_bad_978_file_doesnt_hide_1090(self):
        self.write("a", {"now": 100.0, "aircraft": [PLANE]})
        self.write("u", "{broken")
        self.assertEqual([r.hex for r in self.adapter().fetch().aircraft], ["a1"])

    def test_978_alone_is_used(self):
        self.write("u", {"now": 50.0, "aircraft": [UAT]})
        self.assertEqual([r.hex for r in self.adapter().fetch().aircraft], ["b2"])

    def test_the_paths_match_the_settings_page_test(self):
        php = read("api.php")
        listed = re.findall(r"array\('[^']+', '(/run/[^']+)'\)", php)
        self.assertEqual(listed, list(LOCAL_1090_FILES + LOCAL_978_FILES))


class FeedManagerTests(Files):
    def manager(self, **overrides):
        self.time = 0.0
        values = {"source_mode": "receiver", "receiver_url": LOCAL_RECEIVER, "radius_receiver": 5.0}
        values.update(overrides)
        manager = FeedManager(
            Settings(**values),
            Notifier([WarningSink()]),
            fetch=self.fail_network,
            clock=lambda: self.time,
        )
        adapter = self.adapter()
        manager._local_adapter = lambda: adapter
        return manager

    def fail_network(self, url):
        raise AssertionError("a receiver on this box must not be fetched over the network")

    def test_polls_the_file_and_delivers_aircraft(self):
        manager = self.manager()
        now = 1000.0
        for _ in range(3):
            now += 1.0
            self.write("a", {"now": now, "aircraft": [PLANE]})
            self.time += 1.0
            manager.poll_once()
        state = manager.state()
        self.assertEqual(state.active_feed, FEED_RECEIVER)
        self.assertEqual([r.hex for r in state.aircraft], ["a1"])
        self.assertTrue(manager.data_fresh())

    def test_a_file_that_stops_updating_goes_stale(self):
        manager = self.manager()
        self.write("a", {"now": 1000.0, "aircraft": [PLANE]})
        for _ in range(15):
            self.time += 1.0
            manager.poll_once()
        self.assertFalse(manager.data_fresh())

    def test_no_decoder_means_no_fresh_data(self):
        manager = self.manager()
        for _ in range(10):
            self.time += 1.0
            manager.poll_once()
        self.assertFalse(manager.data_fresh())

    def test_both_mode_health_checks_read_the_file(self):
        manager = self.manager(
            source_mode="both", personal_use_confirmed=True, radius=10.0,
            center_lat=40.0, center_lon=-75.0,
        )
        self.write("a", {"now": 1000.0, "aircraft": [PLANE]})
        self.time = 100.0
        self.assertTrue(manager.health_once())
        self.assertEqual(manager.state().active_feed, FEED_RECEIVER)


if __name__ == "__main__":
    unittest.main()
