import unittest

from skyguard.config import Settings, load_settings
from skyguard.feeds.base import GROUND, AircraftRecord
from skyguard.notifier import Notifier, WarningSink
from skyguard.trigger import NOTE_GROUND_IGNORED, Inputs, TriggerEngine, BLACKOUT, CLEAR, evaluate_zone, is_taxiing

CENTER = (40.0, -75.0)
RADIUS_NM = 10.0
INSIDE = (40.1, -75.0)  # about 6 nm from center
OUTSIDE = (40.2, -75.0)  # about 12 nm from center
LIMIT = 40.0


def plane(hex="a1", position=INSIDE, alt=GROUND, gs=15.0):
    lat, lon = position if position else (None, None)
    return AircraftRecord(hex, None, lat, lon, alt, gs, None, None, "receiver")


def evaluate(records, ignore=True, limit=LIMIT, count_no_position=True):
    return evaluate_zone(
        records,
        CENTER[0],
        CENTER[1],
        RADIUS_NM,
        0.0,
        10000.0,
        count_no_position,
        ignore_ground=ignore,
        ground_speed_limit_kt=limit,
    )


class RequiredBehaviorTests(unittest.TestCase):
    def test_taxiing_on_the_ground_is_omitted(self):
        self.assertEqual(evaluate([plane(gs=15.0)]).in_zone, ())

    def test_parked_on_the_ground_is_omitted(self):
        self.assertEqual(evaluate([plane(gs=0.0)]).in_zone, ())

    def test_reaching_takeoff_speed_on_the_ground_counts(self):
        self.assertEqual(len(evaluate([plane(gs=LIMIT)]).in_zone), 1)

    def test_just_after_takeoff_counts(self):
        self.assertEqual(len(evaluate([plane(alt=150, gs=140.0)]).in_zone), 1)


class SpeedLimitTests(unittest.TestCase):
    def test_limit_is_the_first_speed_that_counts(self):
        self.assertTrue(is_taxiing(plane(gs=39.9), LIMIT))
        self.assertFalse(is_taxiing(plane(gs=40.0), LIMIT))

    def test_limit_is_configurable(self):
        self.assertFalse(is_taxiing(plane(gs=25.0), 20.0))
        self.assertTrue(is_taxiing(plane(gs=55.0), 60.0))

    def test_zone_evaluation_uses_the_configured_limit(self):
        self.assertEqual(len(evaluate([plane(gs=25.0)], limit=20.0).in_zone), 1)
        self.assertEqual(evaluate([plane(gs=55.0)], limit=60.0).in_zone, ())

    def test_ground_with_no_speed_is_omitted(self):
        self.assertTrue(is_taxiing(plane(gs=None), LIMIT))
        self.assertEqual(evaluate([plane(gs=None)]).in_zone, ())

    def test_ground_with_no_speed_counts_once_it_reports_the_limit(self):
        self.assertEqual(evaluate([plane(gs=None)]).in_zone, ())
        self.assertEqual(len(evaluate([plane(gs=LIMIT)]).in_zone), 1)


class AltitudeNoLongerMattersTests(unittest.TestCase):
    def test_slow_aircraft_with_a_numeric_altitude_counts(self):
        # A helicopter: 2 kt at 750 ft, no ground report.
        self.assertEqual(len(evaluate([plane(alt=750, gs=2.0)]).in_zone), 1)

    def test_slow_aircraft_at_a_very_low_altitude_counts(self):
        self.assertEqual(len(evaluate([plane(alt=25, gs=5.0)]).in_zone), 1)

    def test_no_reported_altitude_counts_inside_the_radius(self):
        self.assertEqual(len(evaluate([plane(alt=None, gs=5.0)]).in_zone), 1)

    def test_a_numeric_altitude_is_never_taxiing(self):
        self.assertFalse(is_taxiing(plane(alt=0, gs=5.0), LIMIT))


class HelicopterTests(unittest.TestCase):
    def test_below_the_limit_with_no_ground_report_counts(self):
        for speed in (0.0, 2.0, 34.0, 39.9):
            with self.subTest(speed=speed):
                self.assertEqual(len(evaluate([plane(alt=750, gs=speed)]).in_zone), 1)

    def test_below_the_limit_with_a_ground_report_is_omitted(self):
        self.assertEqual(evaluate([plane(alt=GROUND, gs=2.0)]).in_zone, ())

    def test_no_speed_and_no_ground_report_counts(self):
        self.assertEqual(len(evaluate([plane(alt=750, gs=None)]).in_zone), 1)

    def test_no_altitude_and_no_ground_report_counts(self):
        self.assertEqual(len(evaluate([plane(alt=None, gs=None)]).in_zone), 1)


class DefaultOffTests(unittest.TestCase):
    def test_setting_defaults(self):
        settings = Settings()
        self.assertFalse(settings.ignore_ground)
        self.assertEqual(settings.ground_speed_limit_kt, 40.0)

    def test_off_counts_ground_traffic(self):
        result = evaluate([plane()], ignore=False)
        self.assertEqual(len(result.in_zone), 1)
        self.assertFalse(result.aircraft[0].ground_ignored)

    def test_existing_callers_keep_counting_ground(self):
        result = evaluate_zone([plane()], *CENTER, RADIUS_NM, 0.0, 10000.0, True)
        self.assertEqual(len(result.in_zone), 1)


class NoPositionTests(unittest.TestCase):
    def test_taxiing_with_no_position_is_omitted_even_with_the_toggle_on(self):
        result = evaluate([plane(position=None)], count_no_position=True)
        self.assertEqual(result.in_zone, ())
        self.assertEqual(len(result.ground_skipped), 1)

    def test_it_is_not_also_logged_as_no_position(self):
        result = evaluate([plane(position=None)], count_no_position=False)
        self.assertEqual(result.no_position_skipped, ())
        self.assertEqual(len(result.ground_skipped), 1)

    def test_fast_with_no_position_follows_the_toggle(self):
        self.assertEqual(len(evaluate([plane(position=None, gs=90.0)]).in_zone), 1)
        result = evaluate([plane(position=None, gs=90.0)], count_no_position=False)
        self.assertEqual(result.in_zone, ())
        self.assertEqual(len(result.no_position_skipped), 1)


class StatusAndLogTests(unittest.TestCase):
    def test_omitted_aircraft_stay_in_status_and_are_marked(self):
        result = evaluate([plane()])
        self.assertEqual(len(result.aircraft), 1)
        aircraft = result.aircraft[0]
        self.assertTrue(aircraft.ground_ignored)
        self.assertEqual(aircraft.note, "ground (ignored)")
        self.assertEqual(aircraft.note, NOTE_GROUND_IGNORED)
        self.assertAlmostEqual(aircraft.distance_nm, 6.0, delta=0.1)

    def test_counted_aircraft_have_no_note(self):
        self.assertIsNone(evaluate([plane(alt=3000)]).aircraft[0].note)

    def test_omitted_aircraft_in_the_zone_are_listed_for_the_log(self):
        self.assertEqual([r.hex for r in evaluate([plane()]).ground_skipped], ["a1"])

    def test_outside_the_radius_is_marked_but_not_logged(self):
        result = evaluate([plane(position=OUTSIDE)])
        self.assertTrue(result.aircraft[0].ground_ignored)
        self.assertEqual(result.ground_skipped, ())

    def test_omitted_aircraft_are_never_the_closest(self):
        result = evaluate([plane("g", position=(40.01, -75.0)), plane("a", alt=3000)])
        self.assertEqual(result.closest.record.hex, "a")
        self.assertEqual(len(result.in_zone), 1)

    def test_log_event_is_not_rate_limited_or_a_warning(self):
        warnings = WarningSink()
        notifier = Notifier([warnings])
        self.assertTrue(notifier.emit("ground_aircraft_ignored", {"aircraft": "a1"}))
        self.assertTrue(notifier.emit("ground_aircraft_ignored", {"aircraft": "a2"}))
        self.assertEqual(warnings.warnings(), [])


class TakeoffSequenceTests(unittest.TestCase):
    def step(self, engine, now, record):
        zone = evaluate([record])
        engine.update(now, Inputs(True, True, True, True, tuple(a.record for a in zone.in_zone)))
        return engine.state

    def test_blackout_on_the_poll_it_reaches_takeoff_speed(self):
        engine = TriggerEngine(5.0)
        self.assertEqual(self.step(engine, 0.0, plane(gs=10.0)), CLEAR)
        self.assertEqual(self.step(engine, 1.0, plane(gs=25.0)), CLEAR)
        self.assertEqual(self.step(engine, 2.0, plane(gs=39.0)), CLEAR)
        self.assertEqual(self.step(engine, 3.0, plane(gs=48.0)), BLACKOUT)
        self.assertEqual(self.step(engine, 4.0, plane(alt=100, gs=130.0)), BLACKOUT)

    def test_landing_counts_until_it_slows_then_waits_the_release_delay(self):
        engine = TriggerEngine(5.0)
        self.assertEqual(self.step(engine, 0.0, plane(alt=200, gs=130.0)), BLACKOUT)
        self.assertEqual(self.step(engine, 1.0, plane(gs=90.0)), BLACKOUT)
        self.assertEqual(self.step(engine, 2.0, plane(gs=30.0)), BLACKOUT)
        self.assertEqual(self.step(engine, 6.9, plane(gs=20.0)), BLACKOUT)
        self.assertEqual(self.step(engine, 7.0, plane(gs=15.0)), CLEAR)


class SettingsTests(unittest.TestCase):
    def test_values_are_typed(self):
        s = load_settings({"ignore_ground": "1", "ground_speed_limit_kt": "60"})
        self.assertTrue(s.ignore_ground)
        self.assertEqual(s.ground_speed_limit_kt, 60.0)
        self.assertEqual(s.warnings, ())

    def test_invalid_values_fall_back_with_a_warning(self):
        for key, bad, expected in (
            ("ignore_ground", "maybe", False),
            ("ground_speed_limit_kt", "fast", 40.0),
            ("ground_speed_limit_kt", "1", 40.0),
            ("ground_speed_limit_kt", "500", 40.0),
        ):
            with self.subTest(key=key, bad=bad):
                s = load_settings({key: bad})
                self.assertEqual(getattr(s, key), expected)
                self.assertTrue(any(key in w for w in s.warnings))

    def test_removed_settings_are_gone(self):
        self.assertFalse(hasattr(Settings(), "airport_elevation_ft"))
        self.assertFalse(hasattr(Settings(), "ground_margin_ft"))


if __name__ == "__main__":
    unittest.main()
