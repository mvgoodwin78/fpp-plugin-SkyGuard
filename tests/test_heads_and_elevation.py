"""Heads set by FPP model, and altitude limits measured above the show's elevation."""

import json
import os
import tempfile
import unittest

from skyguard.config import Settings, load_settings
from skyguard.feeds.base import GROUND, AircraftRecord
from skyguard.migrate import universes_to_start_channels
from skyguard.notifier import MODEL_HELD
from skyguard.overlay import (
    MODEL_MISSING,
    ChannelResolver,
    blackout_target,
    model_entries,
    resolve_fixtures,
)
from skyguard.status import _aircraft_entry
from skyguard.trigger import EvaluatedAircraft, evaluate_zone
from tests.test_overlay import Rig, fixture, universe, universes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CENTER = (40.0, -75.0)
NEAR = (40.01, -75.0)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def plane(alt, position=NEAR):
    return AircraftRecord("a1", None, position[0], position[1], alt, None, None, None, "receiver")


def in_zone(alt, floor=0.0, ceiling=1000.0, elevation=0.0):
    result = evaluate_zone([plane(alt)], CENTER[0], CENTER[1], 5.0, floor, ceiling, True, elevation_ft=elevation)
    return bool(result.in_zone)


def model(name, start, count=16, kind="Channel"):
    return {"Name": name, "StartChannel": start, "ChannelCount": count, "Type": kind}


class ElevationTests(unittest.TestCase):
    def test_limits_are_heights_above_the_show(self):
        # Show at 800 ft with a 1,000 ft ceiling: aircraft up to 1,800 ft above sea level count.
        self.assertTrue(in_zone(1750, elevation=800))
        self.assertFalse(in_zone(1850, elevation=800))
        self.assertFalse(in_zone(1850, elevation=0))  # at 0, measured from sea level
        self.assertTrue(in_zone(950, elevation=0))

    def test_a_floor_is_above_the_show_too(self):
        self.assertFalse(in_zone(900, floor=200, elevation=800))  # 100 ft above the show
        self.assertTrue(in_zone(1100, floor=200, elevation=800))

    def test_on_the_ground_is_at_the_shows_level(self):
        self.assertTrue(in_zone(GROUND, elevation=800))

    def test_below_the_show_still_counts_with_no_floor(self):
        # Pressure altitude can read low; with no floor, nothing near is let through for it.
        self.assertTrue(in_zone(600, elevation=800))

    def test_the_setting(self):
        self.assertEqual(Settings().show_elevation_ft, 0.0)
        self.assertEqual(load_settings({"show_elevation_ft": "812"}).show_elevation_ft, 812.0)
        bad = load_settings({"show_elevation_ft": "40000"})
        self.assertEqual(bad.show_elevation_ft, 0.0)
        self.assertTrue(any("show_elevation_ft" in w for w in bad.warnings))

    def test_status_gives_height_above_the_show(self):
        settings = Settings(show_elevation_ft=800.0)
        entry = _aircraft_entry(EvaluatedAircraft(plane(1750), 1.0, True), settings, "receiver")
        self.assertEqual((entry["alt_ft"], entry["height_ft"]), (1750, 950))
        ground = _aircraft_entry(EvaluatedAircraft(plane(GROUND), 1.0, True), settings, "receiver")
        self.assertIsNone(ground["height_ft"])

    def test_the_page_and_privacy_info(self):
        self.assertIn("skyGuardNumber('show_elevation_ft'", read("content.php"))
        self.assertIn("'endpoint' => 'elevation'", read("api.php"))
        self.assertIn("api.open-meteo.com", read("api.php"))
        sends = json.loads(read("pluginInfo.json"))["privacy"]["sends"]
        meteo = [s for s in sends if s["to"] == "api.open-meteo.com"]
        self.assertEqual(len(meteo), 1)
        self.assertFalse(meteo[0]["alwaysOn"])


class ModelRowTests(unittest.TestCase):
    def test_a_model_is_enough(self):
        row = fixture("A", model="Head Left", dimmer_ch=6)
        self.assertTrue(row.complete, row.problems)
        self.assertEqual(row.model, "Head Left")

    def test_only_one_way_to_patch(self):
        self.assertFalse(fixture("A", model="Head", start_channel=5, dimmer_ch=1).complete)
        self.assertFalse(fixture("A", model="Head", universe=1, dimmer_ch=1).complete)
        self.assertFalse(fixture("A", dimmer_ch=1).complete)

    def test_a_bad_model_name_is_a_problem(self):
        self.assertFalse(fixture("A", model="bad\nname", dimmer_ch=1).complete)
        self.assertFalse(fixture("A", model=5, dimmer_ch=1).complete)

    def test_the_page_saves_a_model(self):
        php = read("api.php")
        self.assertIn("$clean['model']", php)
        with open(os.path.join(ROOT, "api.php"), "rb") as handle:
            self.assertNotIn(b"\x00", handle.read())


class ModelResolveTests(unittest.TestCase):
    def test_only_channel_models_with_a_start_count(self):
        entries = model_entries([
            model("Head", 101), model("FB - HDMI-A-1", 0, 62208, "FB"),
            model("Zero", 0), {"Name": "Broken"}, "junk",
        ])
        self.assertEqual(entries, {"Head": (101, 16)})

    def test_channels_come_from_the_model(self):
        rows = (fixture("A", model="Head", dimmer_ch=6, shutter_ch=7, shutter_off=8),)
        result = resolve_fixtures(rows, None, [model("Head", 101)])
        self.assertTrue(result[0].valid, result[0].problem)
        self.assertEqual(blackout_target(result), {106: 0, 107: 8})

    def test_a_channel_past_the_model_is_a_problem(self):
        rows = (fixture("A", model="Head", dimmer_ch=17),)
        result = resolve_fixtures(rows, None, [model("Head", 101, count=16)])
        self.assertFalse(result[0].valid)
        self.assertEqual(blackout_target(result), {})

    def test_a_missing_model_with_no_history_resolves_to_nothing(self):
        result = resolve_fixtures((fixture("A", model="Gone", dimmer_ch=1),), None, [])
        self.assertEqual(result[0].problem, MODEL_MISSING)
        self.assertEqual(blackout_target(result), {})

    def test_a_missing_model_keeps_its_last_channels(self):
        result = resolve_fixtures((fixture("A", model="Gone", dimmer_ch=1),), None, [], {"Gone": (500, 16)})
        self.assertEqual(result[0].problem, MODEL_HELD)
        self.assertFalse(result[0].valid)
        self.assertEqual(blackout_target(result), {500: 0})

    def test_model_and_universe_rows_mix(self):
        rows = (fixture("A", model="Head", dimmer_ch=1), fixture("B", universe=2, dimmer_ch=1))
        result = resolve_fixtures(rows, universes(universe(2, 1025)), [model("Head", 1)])
        self.assertEqual(blackout_target(result), {1: 0, 1025: 0})


class ModelResolverTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.known = os.path.join(self.r.dir.name, "model_channels.json")
        self.rows = (fixture("A", model="Head", dimmer_ch=6),)

    def resolver(self):
        return ChannelResolver(self.r.api, self.r.notifier, self.r.clock, known_path=self.known)

    def resolve(self, resolver):
        resolver.read_outputs(force=True)
        return resolver.resolve(self.rows)

    def test_reads_fpps_models_and_follows_a_repatch(self):
        resolver = self.resolver()
        self.r.fpp.model_list = [model("Head", 101)]
        self.assertEqual(blackout_target(self.resolve(resolver)), {106: 0})
        self.r.fpp.model_list = [model("Head", 301)]  # re-patched in xLights and uploaded again
        self.assertEqual(blackout_target(self.resolve(resolver)), {306: 0})
        self.assertIn(("GET", "/api/overlays/models", None), self.r.fpp.requests)

    def test_a_deleted_model_keeps_its_head_held_even_after_a_restart(self):
        self.r.fpp.model_list = [model("Head", 101)]
        self.resolve(self.resolver())
        self.r.fpp.model_list = []
        restarted = self.resolver()
        result = self.resolve(restarted)
        self.assertEqual(blackout_target(result), {106: 0})
        self.assertEqual(result[0].problem, MODEL_HELD)
        warning = " ".join(self.r.warnings.warnings())
        self.assertIn("keeps holding", warning)
        self.assertNotIn("won't black out", warning)

    def test_a_failed_models_read_keeps_the_last_good_copy(self):
        resolver = self.resolver()
        self.r.fpp.model_list = [model("Head", 101)]
        self.resolve(resolver)
        self.r.fpp.model_list_status = 500
        self.assertEqual(blackout_target(self.resolve(resolver)), {106: 0})
        self.assertIsNone(resolver.resolved[0].problem)

    def test_an_unreadable_saved_file_starts_empty(self):
        with open(self.known, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        with self.assertLogs("skyguard", level="WARNING"):
            resolver = self.resolver()
        self.r.fpp.model_list = []
        self.assertEqual(self.resolve(resolver)[0].problem, MODEL_MISSING)


class MigrateUniverseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fixtures = os.path.join(self.dir, "fixtures.json")
        self.universes = os.path.join(self.dir, "co-universes.json")

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

    def rows(self):
        with open(self.fixtures, encoding="utf-8") as handle:
            return json.load(handle)

    def test_universe_rows_become_the_same_start_channel(self):
        # An output switched off still says where its channels are.
        self.write(self.universes, universes(universe(1, 1), universe(5, 2049), enabled=0))
        self.write(self.fixtures, [
            {"id": "a", "name": "A", "universe": 5, "dimmer_ch": 10},
            {"id": "b", "name": "B", "universe": 9, "dimmer_ch": 1},  # FPP doesn't have it
            {"id": "c", "name": "C", "start_channel": 7, "dimmer_ch": 1},
            {"id": "d", "name": "D", "universe": 1, "start_channel": 3, "dimmer_ch": 1},  # invalid
        ])
        self.assertTrue(universes_to_start_channels(self.fixtures, self.universes))
        rows = {row["id"]: row for row in self.rows()}
        self.assertEqual((rows["a"]["start_channel"], rows["a"]["universe"]), (2049, None))
        self.assertEqual(rows["b"]["universe"], 9)
        self.assertEqual(rows["c"]["start_channel"], 7)
        self.assertEqual((rows["d"]["universe"], rows["d"]["start_channel"]), (1, 3))
        # The converted head still lands on the same FPP channel.
        before = resolve_fixtures((fixture("A", universe=5, dimmer_ch=10),), universes(universe(5, 2049)))
        after = resolve_fixtures((fixture("A", start_channel=2049, dimmer_ch=10),), None)
        self.assertEqual(blackout_target(before), blackout_target(after))
        self.assertFalse(universes_to_start_channels(self.fixtures, self.universes))

    def test_no_output_file_changes_nothing(self):
        self.write(self.fixtures, [{"id": "a", "name": "A", "universe": 5, "dimmer_ch": 10}])
        self.assertFalse(universes_to_start_channels(self.fixtures, self.universes))


if __name__ == "__main__":
    unittest.main()
