import json
import os
import tempfile
import unittest

from skyguard.config import (
    ConfigStore,
    Paths,
    Settings,
    load_settings,
    parse_fixture,
    parse_fixtures,
    parse_plugin_config,
)


class ParseConfigTests(unittest.TestCase):
    def test_fpp_format(self):
        values = parse_plugin_config('enabled = "1"\nradius = "12.5"\n')
        self.assertEqual(values, {"enabled": "1", "radius": "12.5"})

    def test_equals_inside_a_value_is_kept(self):
        values = parse_plugin_config('receiver_url = "http://rx/data.json?a=1"\n')
        self.assertEqual(values["receiver_url"], "http://rx/data.json?a=1")

    def test_blank_comment_and_junk_lines_are_ignored(self):
        self.assertEqual(parse_plugin_config('\n# note\n; note\njunk\na = "1"\n'), {"a": "1"})

    def test_unquoted_values_are_accepted(self):
        self.assertEqual(parse_plugin_config("a = 5"), {"a": "5"})

    def test_empty_value(self):
        self.assertEqual(parse_plugin_config('a = ""'), {"a": ""})

    def test_non_ascii_message_survives(self):
        values = parse_plugin_config('m = "Data offline – lights paused"')
        self.assertEqual(values["m"], "Data offline – lights paused")


class SettingsDefaultsTests(unittest.TestCase):
    def test_spec_defaults(self):
        s = load_settings({})
        self.assertTrue(s.enabled)
        self.assertEqual(s.source_mode, "receiver")  # the local option; nothing is sent until online is chosen
        self.assertFalse(s.online_between_shows)
        self.assertEqual(s.online_provider, "adsb.fi")
        self.assertEqual(s.poll_interval_s, 1.0)
        self.assertEqual(s.online_idle_interval_s, 30.0)
        self.assertEqual(s.health_interval_s, 10.0)
        self.assertEqual(s.health_fail_count, 2)
        self.assertEqual(s.max_data_age_s, 5.0)
        self.assertEqual((s.radius, s.radius_receiver, s.radius_unit), (None, None, "mi"))
        self.assertEqual((s.alt_floor_ft, s.alt_ceiling_ft), (0.0, 10000.0))
        self.assertTrue(s.count_no_position)
        self.assertEqual(s.release_delay_s, 5.0)
        self.assertFalse(s.matrix_enabled)
        self.assertEqual(s.matrix_message, "Aircraft overhead: {callsign} at {altitude} ft")
        self.assertEqual(s.matrix_nodata_message, "Aircraft data offline – lights paused")
        self.assertEqual(s.warnings, ())

    def test_center_starts_unset(self):
        self.assertFalse(load_settings({}).center_set)


class SettingsValidationTests(unittest.TestCase):
    def test_values_are_typed(self):
        s = load_settings({"enabled": "0", "radius": "12.5", "radius_unit": "km", "health_fail_count": "3"})
        self.assertFalse(s.enabled)
        self.assertEqual(s.radius, 12.5)
        self.assertEqual(s.radius_unit, "km")
        self.assertEqual(s.health_fail_count, 3)

    def test_radius_converts_to_nm(self):
        self.assertAlmostEqual(load_settings({"radius": "1.852", "radius_unit": "km"}).radius_nm, 1.0)

    def assert_falls_back(self, key, bad_value):
        s = load_settings({key: bad_value})
        self.assertEqual(getattr(s, key), getattr(Settings(), key), key)
        self.assertTrue(any(key in w for w in s.warnings), key)

    def test_invalid_values_fall_back_with_a_warning(self):
        for key, bad in (
            ("poll_interval_s", "0.2"),
            ("poll_interval_s", "3"),
            ("online_idle_interval_s", "1"),
            ("max_data_age_s", "-1"),
            ("release_delay_s", "-1"),
            ("radius", "0"),
            ("radius", "abc"),
            ("radius", "nan"),
            ("radius", "inf"),
            ("radius_unit", "furlongs"),
            ("source_mode", "everything"),
            ("online_provider", "unknown.example"),
            ("enabled", "maybe"),
            ("health_fail_count", "1.5"),
            ("geocoder", "someone-else"),
            ("matrix_size", "1000"),
        ):
            with self.subTest(key=key, bad=bad):
                self.assert_falls_back(key, bad)

    def test_a_saved_removed_provider_falls_back_with_a_warning(self):
        s = load_settings({"online_provider": "airplanes.live"})
        self.assertEqual(s.online_provider, "adsb.fi")
        self.assertTrue(any("online_provider" in w for w in s.warnings))

    def test_zero_is_allowed_where_the_spec_gives_it_meaning(self):
        s = load_settings({"max_data_age_s": "0", "release_delay_s": "0"})
        self.assertEqual((s.max_data_age_s, s.release_delay_s), (0.0, 0.0))
        self.assertEqual(s.warnings, ())

    def test_floor_above_ceiling_uses_the_default_band(self):
        s = load_settings({"alt_floor_ft": "5000", "alt_ceiling_ft": "1000"})
        self.assertEqual((s.alt_floor_ft, s.alt_ceiling_ft), (0.0, 10000.0))
        self.assertTrue(s.warnings)

    def test_radius_beyond_the_online_cap_warns(self):
        self.assertTrue(load_settings({"radius": "300", "radius_unit": "nm"}).warnings)

    def test_matrix_models_become_a_list(self):
        s = load_settings({"matrix_models": "Panel A, Panel B,,"})
        self.assertEqual(s.matrix_models, ("Panel A", "Panel B"))

    def test_blank_nodata_message_means_none(self):
        self.assertEqual(load_settings({"matrix_nodata_message": ""}).matrix_nodata_message, "")

    def test_only_plain_http_urls_are_allowed_for_the_receiver(self):
        self.assertEqual(
            load_settings({"receiver_url": "http://10.0.0.5/data/aircraft.json"}).receiver_url,
            "http://10.0.0.5/data/aircraft.json",
        )
        for bad in (
            "file:///etc/passwd",
            "gopher://x/",
            "http://",
            "not a url",
            "http://host/a\nb",
        ):
            with self.subTest(bad=bad):
                s = load_settings({"receiver_url": bad})
                self.assertEqual(s.receiver_url, "")
                self.assertTrue(s.warnings)


class PrivateValueTests(unittest.TestCase):
    def test_center_and_key_come_from_private_values(self):
        s = load_settings({}, {"center_lat": 40.5, "center_lon": -75.5, "online_api_key": "k"})
        self.assertTrue(s.center_set)
        self.assertEqual((s.center_lat, s.center_lon), (40.5, -75.5))
        self.assertEqual(s.online_api_key, "k")

    def test_private_values_in_the_plugin_config_are_ignored(self):
        s = load_settings({"center_lat": "40.5", "center_lon": "-75.5", "online_api_key": "k"})
        self.assertFalse(s.center_set)
        self.assertEqual(s.online_api_key, "")

    def test_invalid_or_partial_center_is_unset_with_a_warning(self):
        for private in (
            {"center_lat": 91, "center_lon": 0},
            {"center_lat": 0, "center_lon": 181},
            {"center_lat": 40.5},
            {"center_lat": "abc", "center_lon": "def"},
            {"center_lat": True, "center_lon": 0},
        ):
            with self.subTest(private=private):
                s = load_settings({}, private)
                self.assertFalse(s.center_set)
                self.assertTrue(s.warnings)

    def test_zero_zero_is_a_valid_center(self):
        self.assertTrue(load_settings({}, {"center_lat": 0, "center_lon": 0}).center_set)

    def test_warnings_never_contain_private_or_invalid_values(self):
        s = load_settings(
            {"receiver_url": "http://user:hunter2@/x y", "radius": "hunter2"},
            {"center_lat": "hunter2", "center_lon": 0, "online_api_key": "hunter2"},
        )
        self.assertTrue(s.warnings)
        self.assertNotIn("hunter2", " ".join(s.warnings))


class FixtureTests(unittest.TestCase):
    def test_universe_fixture(self):
        f = parse_fixture({"name": "Roof left", "universe": 3, "dimmer_ch": 10, "shutter_ch": 11, "shutter_off": 8})
        self.assertTrue(f.active)
        self.assertEqual((f.universe, f.dimmer_ch, f.dimmer_off, f.shutter_ch, f.shutter_off), (3, 10, 0, 11, 8))

    def test_start_channel_fixture(self):
        f = parse_fixture({"name": "USB head", "start_channel": 5001, "dimmer_ch": 1})
        self.assertTrue(f.active)
        self.assertEqual(f.start_channel, 5001)

    def test_values_saved_as_strings_are_accepted(self):
        f = parse_fixture({"name": "H", "universe": "3", "dimmer_ch": "10", "dimmer_off": "5", "enabled": "true"})
        self.assertTrue(f.active)
        self.assertEqual((f.universe, f.dimmer_ch, f.dimmer_off), (3, 10, 5))

    def test_either_channel_alone_is_enough(self):
        self.assertTrue(parse_fixture({"name": "H", "universe": 1, "shutter_ch": 2}).active)
        self.assertTrue(parse_fixture({"name": "H", "universe": 1, "dimmer_ch": 2}).active)

    def test_incomplete_rows_are_ignored(self):
        for row in (
            {},
            {"name": "H"},
            {"name": "H", "universe": 1},
            {"name": "H", "dimmer_ch": 1},
            {"universe": 1, "dimmer_ch": 1},
            {"name": "  ", "universe": 1, "dimmer_ch": 1},
            {"name": "H", "universe": 1, "start_channel": 1, "dimmer_ch": 1},
            {"name": "H", "universe": "", "dimmer_ch": ""},
            "not a row",
        ):
            with self.subTest(row=row):
                f = parse_fixture(row)
                self.assertFalse(f.complete)
                self.assertFalse(f.active)

    def test_out_of_range_values_are_never_clamped_onto_another_channel(self):
        for field, bad in (
            ("dimmer_ch", 0),
            ("dimmer_ch", 513),
            ("shutter_ch", 513),
            ("dimmer_off", 256),
            ("dimmer_off", -1),
            ("universe", -1),
            ("dimmer_ch", 1.5),
            ("dimmer_ch", True),
            ("dimmer_ch", "abc"),
        ):
            with self.subTest(field=field, bad=bad):
                row = {"name": "H", "universe": 1, "dimmer_ch": 1, field: bad}
                self.assertFalse(parse_fixture(row).complete)

    def test_universe_zero_is_valid_for_art_net(self):
        self.assertTrue(parse_fixture({"name": "H", "universe": 0, "dimmer_ch": 1}).active)

    def test_a_disabled_row_is_complete_but_not_active(self):
        f = parse_fixture({"name": "H", "universe": 1, "dimmer_ch": 1, "enabled": False})
        self.assertTrue(f.complete)
        self.assertFalse(f.active)

    def test_missing_enabled_means_protected(self):
        self.assertTrue(parse_fixture({"name": "H", "universe": 1, "dimmer_ch": 1}).enabled)

    def test_list_must_be_an_array(self):
        with self.assertRaises(ValueError):
            parse_fixtures({"name": "H"})


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        media = self.dir.name
        self.paths = Paths.from_env({"MEDIADIR": media})
        os.makedirs(os.path.dirname(self.paths.config_file))
        os.makedirs(self.paths.data_dir)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        # Force a visible change even within the filesystem's timestamp resolution.
        info = os.stat(path)
        self._tick = getattr(self, "_tick", 0) + 1
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + self._tick * 1_000_000_000))

    def test_paths_follow_the_environment(self):
        paths = Paths.from_env({"MEDIADIR": "/m", "LOGDIR": "/l"})
        self.assertEqual(paths.log_file.replace("\\", "/"), "/l/plugin-fpp-plugin-SkyGuard.log")
        self.assertEqual(
            paths.config_file.replace("\\", "/"), "/m/config/plugin.fpp-plugin-SkyGuard"
        )
        self.assertEqual(
            paths.private_file.replace("\\", "/"),
            "/m/plugindata/fpp-plugin-SkyGuard/private.json",
        )

    def test_paths_default_to_the_fpp_media_dir(self):
        paths = Paths.from_env({})
        self.assertEqual(
            paths.log_file.replace("\\", "/"),
            "/home/fpp/media/logs/plugin-fpp-plugin-SkyGuard.log",
        )

    def test_missing_files_give_defaults(self):
        store = ConfigStore(self.paths)
        self.assertIsNone(store.settings.radius)
        self.assertEqual(store.fixtures, ())
        self.assertEqual(store.warnings, ())

    def test_loads_all_three_files(self):
        self.write(self.paths.config_file, 'radius = "7"\n')
        self.write(self.paths.private_file, json.dumps({"center_lat": 1, "center_lon": 2}))
        self.write(self.paths.fixtures_file, json.dumps([{"name": "H", "universe": 1, "dimmer_ch": 1}]))
        store = ConfigStore(self.paths)
        self.assertEqual(store.settings.radius, 7.0)
        self.assertTrue(store.settings.center_set)
        self.assertEqual(len(store.fixtures), 1)

    def test_reload_reports_changes_only(self):
        self.write(self.paths.config_file, 'radius = "7"\n')
        store = ConfigStore(self.paths)
        self.assertFalse(store.reload())
        self.write(self.paths.config_file, 'radius = "8"\n')
        self.assertTrue(store.reload())
        self.assertEqual(store.settings.radius, 8.0)
        self.assertFalse(store.reload())

    def test_corrupt_fixture_file_keeps_the_last_good_list(self):
        self.write(self.paths.fixtures_file, json.dumps([{"name": "H", "universe": 1, "dimmer_ch": 1}]))
        store = ConfigStore(self.paths)
        self.write(self.paths.fixtures_file, "{not json")
        store.reload()
        self.assertEqual(len(store.fixtures), 1)
        self.assertTrue(any("fixtures.json" in w for w in store.warnings))

    def test_warning_clears_when_the_file_is_fixed(self):
        self.write(self.paths.fixtures_file, "{not json")
        store = ConfigStore(self.paths)
        self.assertTrue(store.warnings)
        self.write(self.paths.fixtures_file, "[]")
        store.reload()
        self.assertEqual(store.warnings, ())

    def test_corrupt_private_file_keeps_the_last_good_center(self):
        self.write(self.paths.private_file, json.dumps({"center_lat": 1, "center_lon": 2}))
        store = ConfigStore(self.paths)
        self.write(self.paths.private_file, "{not json")
        store.reload()
        self.assertTrue(store.settings.center_set)
        self.assertTrue(any("private.json" in w for w in store.warnings))

    def test_fixture_array_of_wrong_type_keeps_the_last_good_list(self):
        self.write(self.paths.fixtures_file, json.dumps([{"name": "H", "universe": 1, "dimmer_ch": 1}]))
        store = ConfigStore(self.paths)
        self.write(self.paths.fixtures_file, json.dumps({"oops": True}))
        store.reload()
        self.assertEqual(len(store.fixtures), 1)


if __name__ == "__main__":
    unittest.main()
