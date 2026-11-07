import json
import os
import unittest

from skyguard.feeds.base import GROUND, FeedError, FeedSnapshot, parse_entry
from skyguard.feeds.online_adsbx_v2 import PROVIDERS, build_url, parse_online, query_radius_nm
from skyguard.feeds.receiver import SOURCE_RECEIVER_978, merge_snapshots, parse_receiver

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def by_hex(snapshot):
    return {record.hex: record for record in snapshot.aircraft}


class ReadsbTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = parse_receiver(load("receiver_readsb.json"))
        self.records = by_hex(self.snapshot)

    def test_now_is_kept_in_seconds(self):
        self.assertEqual(self.snapshot.now_s, 1793900000.1)

    def test_normal_aircraft_is_fully_normalized(self):
        record = self.records["a00001"]
        self.assertEqual(record.callsign, "TEST101")
        self.assertEqual((record.lat, record.lon), (40.01, -75.01))
        self.assertEqual(record.alt_ft, 3200)
        self.assertEqual(record.gs_kt, 142.5)
        self.assertEqual(record.track_deg, 270.1)
        self.assertEqual(record.seen_pos_s, 0.4)
        self.assertEqual(record.source, "receiver")

    def test_hex_is_lowercased(self):
        self.assertIn("a00001", self.records)

    def test_ground_altitude(self):
        self.assertEqual(self.records["a00002"].alt_ft, GROUND)

    def test_altitude_without_position(self):
        record = self.records["a00003"]
        self.assertFalse(record.has_position)
        self.assertEqual(record.alt_ft, 4500)

    def test_last_position_is_used_when_lat_lon_have_aged_out(self):
        record = self.records["a00004"]
        self.assertEqual((record.lat, record.lon), (40.02, -75.02))
        self.assertEqual(record.seen_pos_s, 75.0)

    def test_non_icao_hex_keeps_its_prefix(self):
        self.assertIn("~a00005", self.records)

    def test_rough_receiver_estimate_is_not_a_position(self):
        self.assertFalse(self.records["a00006"].has_position)

    def test_entry_without_hex_is_skipped_and_counted(self):
        self.assertEqual(self.snapshot.skipped, 1)
        self.assertEqual(len(self.snapshot.aircraft), 6)


class OtherReceiverFormatTests(unittest.TestCase):
    def test_dump1090fa(self):
        records = by_hex(parse_receiver(load("receiver_dump1090fa.json")))
        self.assertEqual(records["b00001"].alt_ft, 8000)
        self.assertEqual(records["b00002"].alt_ft, GROUND)
        self.assertFalse(records["b00002"].has_position)

    def test_dump978fa(self):
        records = by_hex(parse_receiver(load("receiver_dump978fa.json"), SOURCE_RECEIVER_978))
        self.assertEqual(records["a00003"].callsign, "N978UA")
        self.assertEqual(records["c00001"].alt_ft, GROUND)
        self.assertIsNone(records["c00001"].gs_kt)
        self.assertEqual(records["c00001"].source, SOURCE_RECEIVER_978)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.primary = parse_receiver(load("receiver_readsb.json"))
        self.secondary = parse_receiver(load("receiver_dump978fa.json"), SOURCE_RECEIVER_978)
        self.merged = merge_snapshots(self.primary, self.secondary)
        self.records = by_hex(self.merged)

    def test_lists_are_combined_by_hex(self):
        self.assertEqual(len(self.merged.aircraft), 7)  # six + 978's one new, one shared

    def test_a_position_beats_no_position(self):
        record = self.records["a00003"]  # 1090 has altitude only; 978 has a position
        self.assertEqual((record.lat, record.lon), (40.015, -75.015))
        self.assertEqual(record.source, SOURCE_RECEIVER_978)

    def test_the_fresher_position_wins_and_gaps_are_filled(self):
        a = parse_entry({"hex": "e1", "lat": 1.0, "lon": 1.0, "seen_pos": 9.0, "flight": "OLD"}, "receiver")
        b = parse_entry({"hex": "e1", "lat": 2.0, "lon": 2.0, "seen_pos": 1.0, "gs": 50.0}, "receiver_978")
        merged = merge_snapshots(FeedSnapshot((a,), 1.0), FeedSnapshot((b,), 2.0))
        record = merged.aircraft[0]
        self.assertEqual((record.lat, record.lon), (2.0, 2.0))
        self.assertEqual(record.gs_kt, 50.0)
        self.assertEqual(record.callsign, "OLD")

    def test_now_stays_the_primary_feeds(self):
        self.assertEqual(self.merged.now_s, self.primary.now_s)

    def test_skipped_counts_add_up(self):
        self.assertEqual(self.merged.skipped, 1)


class BadDocumentTests(unittest.TestCase):
    def assert_rejected(self, doc):
        with self.assertRaises(FeedError):
            parse_receiver(doc)

    def test_not_an_object(self):
        self.assert_rejected([])

    def test_missing_list(self):
        self.assert_rejected({"now": 1.0})

    def test_null_list(self):
        self.assert_rejected({"now": 1.0, "aircraft": None})

    def test_missing_now(self):
        self.assert_rejected({"aircraft": []})

    def test_non_numeric_now(self):
        self.assert_rejected({"now": "soon", "aircraft": []})

    def test_empty_list_is_valid_and_means_no_aircraft(self):
        self.assertEqual(parse_receiver({"now": 1.0, "aircraft": []}).aircraft, ())


class BadEntryTests(unittest.TestCase):
    def parse_one(self, **fields):
        return parse_entry({"hex": "aa", **fields}, "receiver")

    def test_non_object_entries_are_skipped_without_losing_the_rest(self):
        snapshot = parse_receiver({"now": 1.0, "aircraft": [None, 5, "x", {"hex": "aa"}]})
        self.assertEqual(snapshot.skipped, 3)
        self.assertEqual(len(snapshot.aircraft), 1)

    def test_out_of_range_position_is_no_position(self):
        self.assertFalse(self.parse_one(lat=91.0, lon=0.0).has_position)
        self.assertFalse(self.parse_one(lat=0.0, lon=181.0).has_position)

    def test_half_a_position_is_no_position(self):
        self.assertFalse(self.parse_one(lat=10.0).has_position)

    def test_nan_and_infinity_are_not_numbers(self):
        record = self.parse_one(lat=float("nan"), lon=0.0, alt_baro=float("inf"), gs=float("nan"))
        self.assertFalse(record.has_position)
        self.assertIsNone(record.alt_ft)
        self.assertIsNone(record.gs_kt)

    def test_booleans_are_not_numbers(self):
        self.assertIsNone(self.parse_one(alt_baro=True).alt_ft)

    def test_unknown_altitude_text_is_not_reported(self):
        self.assertIsNone(self.parse_one(alt_baro="airborne").alt_ft)

    def test_ground_is_case_insensitive(self):
        self.assertEqual(self.parse_one(alt_baro="GROUND").alt_ft, GROUND)

    def test_zero_is_a_real_position_and_altitude(self):
        record = self.parse_one(lat=0.0, lon=0.0, alt_baro=0)
        self.assertTrue(record.has_position)
        self.assertEqual(record.alt_ft, 0)

    def test_category_is_kept_for_the_map(self):
        self.assertEqual(self.parse_one(category="A7").category, "A7")
        self.assertEqual(self.parse_one(category=" a7 ").category, "A7")

    def test_a_missing_or_unusable_category_is_none(self):
        self.assertIsNone(self.parse_one().category)
        for value in ("", "helicopter", "A77", "<b>", 7, None, ["A7"]):
            self.assertIsNone(self.parse_one(category=value).category, value)

    def test_merging_receivers_fills_in_a_missing_category(self):
        with_position = {"hex": "abc123", "lat": 40.0, "lon": -75.0, "seen_pos": 0.1}
        first = parse_receiver({"now": 1793900000, "aircraft": [with_position]})
        second = parse_receiver({"now": 1793900000, "aircraft": [{"hex": "abc123", "category": "A7"}]})
        merged = by_hex(merge_snapshots(first, second))["abc123"]
        self.assertEqual(merged.category, "A7")
        self.assertEqual(merged.lat, 40.0)

    def test_blank_callsign_is_none(self):
        self.assertIsNone(self.parse_one(flight="        ").callsign)

    def test_blank_hex_is_skipped(self):
        self.assertIsNone(parse_entry({"hex": "   "}, "receiver"))


class OnlineTests(unittest.TestCase):
    def test_list_key_milliseconds_and_source(self):
        snapshot = parse_online(load("online_adsbx_v2.json"), "adsb.lol")
        self.assertEqual(snapshot.now_s, 1793900934.0)
        records = by_hex(snapshot)
        self.assertEqual(records["d00001"].callsign, "TEST505")
        self.assertEqual(records["d00001"].source, "online:adsb.lol")
        self.assertEqual(records["d00002"].alt_ft, GROUND)

    def test_provider_error_is_a_failed_poll(self):
        with self.assertRaises(FeedError):
            parse_online({"ac": [], "msg": "rate limited", "now": 1793900934000}, "adsb.lol")

    def test_missing_list_is_a_failed_poll(self):
        with self.assertRaises(FeedError):
            parse_online({"msg": "No error", "now": 1793900934000}, "adsb.lol")

    def test_receiver_shaped_document_is_a_failed_poll(self):
        with self.assertRaises(FeedError):
            parse_online(load("receiver_dump1090fa.json"), "adsb.lol")

    def test_urls_match_the_verified_endpoints(self):
        self.assertEqual(
            build_url("adsb.lol", 40.12346, -75.5, 20),
            "https://api.adsb.lol/v2/point/40.1235/-75.5000/20",
        )
        self.assertEqual(
            build_url("adsb.fi", 40.0, -75.0, 20),
            "https://opendata.adsb.fi/api/v3/lat/40.0000/lon/-75.0000/dist/20",
        )

    def test_only_providers_that_allow_this_access_are_listed(self):
        self.assertEqual(sorted(PROVIDERS), ["adsb.fi", "adsb.lol"])

    def test_every_provider_uses_https_and_a_minimum_interval(self):
        for provider in PROVIDERS.values():
            self.assertTrue(provider.url_template.startswith("https://"), provider.name)
            self.assertGreaterEqual(provider.min_interval_s, 1.0)

    def test_query_radius_rounds_up_and_caps(self):
        self.assertEqual(query_radius_nm(17.38), 18)
        self.assertEqual(query_radius_nm(0.2), 1)
        self.assertEqual(query_radius_nm(900), 250)


if __name__ == "__main__":
    unittest.main()
