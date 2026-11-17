"""The zone log: who passed through each zone, at what altitude, and for how long."""

import json
import os
import shutil
import tempfile
import unittest

from skyguard.feeds.base import GROUND, AircraftRecord
from skyguard.migrate import radius_to_zones
from skyguard.config import load_settings, parse_plugin_config
from skyguard.trigger import evaluate_zone
from skyguard.visits import MAX_AGE_DAYS, VISIT_GAP_S, VisitLog
from tests.zones import box

CENTER = (40.0, -75.0)
INSIDE = (40.05, -75.0)  # 3 nm north
OUTSIDE = (40.5, -75.0)


def plane(hex="a1", position=INSIDE, alt=3000, callsign="TEST1"):
    lat, lon = position
    return AircraftRecord(hex, callsign, lat, lon, alt, 120.0, None, None, "receiver")


class Clock:
    def __init__(self):
        self.now = 100.0

    def mono(self):
        return self.now

    def wall(self):
        return 1793900000.0 + self.now


class VisitLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "zone_visits.jsonl")
        self.clock = Clock()
        self.log = VisitLog(self.path, self.clock.mono, self.clock.wall)
        self.zones = (box(5.0, 0.0, 5000.0), box(5.0, 0.0, 9000.0, zone_id="receiver2"))

    def step(self, records, seconds=1.0, fresh=True):
        self.clock.now += seconds
        self.log.update(evaluate_zone(records, *CENTER, self.zones, True), fresh)

    def written(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                return [json.loads(line) for line in handle]
        except FileNotFoundError:
            return []

    def test_a_visit_records_callsign_altitude_and_time_inside_each_zone(self):
        self.step([plane(alt=3000)])
        for alt in (3500, 6000, 2500):
            self.step([plane(alt=alt)], 10)
        self.assertEqual(sorted(v["zone"] for v in self.log.open_visits()), ["receiver1", "receiver2"])
        self.step([], VISIT_GAP_S + 1)
        visits = {v["zone"]: v for v in self.written()}
        self.assertEqual(set(visits), {"receiver1", "receiver2"})
        whole = visits["receiver2"]
        self.assertEqual((whole["callsign"], whole["hex"], whole["seconds"]), ("TEST1", "a1", 30))
        self.assertEqual((whole["alt_entry_ft"], whole["alt_min_ft"], whole["alt_max_ft"]), (3000, 2500, 6000))
        # Above zone 1's 5,000 ft ceiling for a while: that zone's visit ends once the gap passes.
        self.assertEqual(visits["receiver1"]["alt_max_ft"], 3500)
        self.assertEqual(self.log.open_visits(), [])

    def test_a_short_gap_does_not_split_a_visit(self):
        self.step([plane()])
        self.step([], VISIT_GAP_S - 1)
        self.step([plane()], 1)
        self.step([], VISIT_GAP_S + 1)
        self.assertEqual(len([v for v in self.written() if v["zone"] == "receiver1"]), 1)

    def test_outside_every_zone_is_not_logged(self):
        self.step([plane(position=OUTSIDE)])
        self.step([], VISIT_GAP_S + 1)
        self.assertEqual(self.written(), [])

    def test_stale_data_does_not_keep_a_visit_open(self):
        self.step([plane()])
        for _ in range(int(VISIT_GAP_S) + 2):
            self.step([plane()], 1, fresh=False)
        self.assertEqual(len(self.written()), 2)

    def test_a_callsign_that_arrives_later_is_kept(self):
        self.step([plane(callsign=None)])
        self.step([plane(callsign="LATE1")], 1)
        self.assertEqual({v["callsign"] for v in self.log.open_visits()}, {"LATE1"})

    def test_ground_is_lowest(self):
        self.step([plane(alt=GROUND)])
        self.step([plane(alt=800)], 1)
        visit = self.log.open_visits()[0]
        self.assertEqual((visit["alt_entry_ft"], visit["alt_min_ft"], visit["alt_max_ft"]), (GROUND, GROUND, 800))

    def test_a_clean_stop_saves_the_open_visits(self):
        self.step([plane()])
        self.log.close_all()
        self.assertEqual(len(self.written()), 2)
        self.assertEqual(self.log.open_visits(), [])

    def test_old_visits_and_broken_lines_are_trimmed(self):
        old = {"zone": "receiver1", "hex": "old", "left": self.clock.wall() - (MAX_AGE_DAYS + 1) * 86400}
        new = {"zone": "receiver1", "hex": "new", "left": self.clock.wall()}
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(old) + "\n" + json.dumps(new) + "\n" + '{"zone": "rec')
        self.log.trim(self.clock.wall())
        self.assertEqual([v["hex"] for v in self.written()], ["new"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "plugin.fpp-plugin-SkyGuard")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_each_radius_becomes_the_same_size_box_and_the_band_is_copied(self):
        self.write('radius = "13"\nradius_receiver = "3"\nalt_floor_ft = "100"\nalt_ceiling_ft = "4000"\n')
        before = load_settings(parse_plugin_config(self.read()))
        self.assertTrue(radius_to_zones(self.path))
        raw = parse_plugin_config(self.read())
        self.assertEqual([raw[f"zone1_{side}"] for side in ("north", "south", "east", "west")], ["3"] * 4)
        self.assertEqual(raw["online_zone_west"], "13")
        self.assertEqual((raw["zone2_alt_floor_ft"], raw["online_zone_alt_ceiling_ft"]), ("100", "4000"))
        self.assertEqual(load_settings(raw).zones, before.zones)
        self.assertFalse(radius_to_zones(self.path))  # runs on every update; only once changes anything

    def test_a_zone_already_entered_is_left_alone(self):
        self.write('radius_receiver = "3"\nzone1_north = "1"\n')
        radius_to_zones(self.path)
        self.assertNotIn("zone1_south", parse_plugin_config(self.read()))

    def test_a_last_line_without_a_newline_is_kept_whole(self):
        self.write('radius = "5"')
        radius_to_zones(self.path)
        raw = parse_plugin_config(self.read())
        self.assertEqual((raw["radius"], raw["online_zone_north"]), ("5", "5"))

    def test_no_file_is_no_change(self):
        self.assertFalse(radius_to_zones(os.path.join(self.dir, "missing")))


if __name__ == "__main__":
    unittest.main()
