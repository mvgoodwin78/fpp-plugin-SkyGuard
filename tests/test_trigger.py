import unittest
from dataclasses import replace

from skyguard.feeds.base import GROUND, AircraftRecord
from skyguard.geo import haversine_nm
from skyguard.trigger import (
    BLACKOUT,
    CLEAR,
    IDLE,
    REASON_AIRCRAFT,
    REASON_CLEAR,
    REASON_DISABLED,
    REASON_NO_CENTER,
    REASON_NO_DATA,
    REASON_RESUME,
    REASON_SCHEDULE,
    REASON_TEST,
    Inputs,
    TriggerEngine,
    data_is_fresh,
    evaluate_zone,
)

CENTER = (40.0, -75.0)
RADIUS_NM = 10.0
# One degree of latitude is about 60 nm, so these are about 6 nm and 12 nm from center.
INSIDE = (40.1, -75.0)
OUTSIDE = (40.2, -75.0)


def plane(hex="a1", position=INSIDE, alt=3000):
    lat, lon = position if position else (None, None)
    return AircraftRecord(hex, None, lat, lon, alt, None, None, None, "receiver")


def evaluate(records, floor=0.0, ceiling=10000.0, count_no_position=True, radius=RADIUS_NM):
    return evaluate_zone(records, CENTER[0], CENTER[1], radius, floor, ceiling, count_no_position)


class DistanceRuleTests(unittest.TestCase):
    def test_a_helicopter_is_judged_exactly_like_a_plane(self):
        for position, alt in ((INSIDE, 3000), (OUTSIDE, 3000), (INSIDE, 10001), (None, 3000)):
            base = plane(position=position, alt=alt)
            helicopter = replace(base, category="A7")
            self.assertEqual(len(evaluate([helicopter]).in_zone), len(evaluate([base]).in_zone), (position, alt))

    def test_inside_radius_within_band(self):
        self.assertEqual(len(evaluate([plane()]).in_zone), 1)

    def test_outside_radius(self):
        self.assertEqual(evaluate([plane(position=OUTSIDE)]).in_zone, ())

    def test_exactly_on_the_radius_counts(self):
        distance = haversine_nm(*CENTER, *INSIDE)
        self.assertEqual(len(evaluate([plane()], radius=distance).in_zone), 1)

    def test_distance_is_reported(self):
        result = evaluate([plane()])
        self.assertAlmostEqual(result.aircraft[0].distance_nm, 6.0, delta=0.1)

    def test_every_aircraft_is_reported_even_outside_the_zone(self):
        result = evaluate([plane("a1"), plane("a2", position=OUTSIDE)])
        self.assertEqual(len(result.aircraft), 2)
        self.assertEqual(len(result.in_zone), 1)


class AltitudeRuleTests(unittest.TestCase):
    def test_above_ceiling_does_not_count(self):
        self.assertEqual(evaluate([plane(alt=10001)]).in_zone, ())

    def test_below_floor_does_not_count(self):
        self.assertEqual(evaluate([plane(alt=999)], floor=1000.0).in_zone, ())

    def test_band_is_inclusive(self):
        self.assertEqual(len(evaluate([plane(alt=10000)]).in_zone), 1)
        self.assertEqual(len(evaluate([plane(alt=1000)], floor=1000.0).in_zone), 1)

    def test_ground_with_default_floor_counts(self):
        self.assertEqual(len(evaluate([plane(alt=GROUND)]).in_zone), 1)

    def test_ground_with_a_raised_floor_does_not_count(self):
        self.assertEqual(evaluate([plane(alt=GROUND)], floor=500.0).in_zone, ())

    def test_inside_radius_with_no_altitude_counts(self):
        self.assertEqual(len(evaluate([plane(alt=None)]).in_zone), 1)

    def test_outside_radius_with_no_altitude_does_not_count(self):
        self.assertEqual(evaluate([plane(position=OUTSIDE, alt=None)]).in_zone, ())


class NoPositionRuleTests(unittest.TestCase):
    def test_counts_when_toggle_is_on(self):
        result = evaluate([plane(position=None)], count_no_position=True)
        self.assertEqual(len(result.in_zone), 1)
        self.assertIsNone(result.aircraft[0].distance_nm)

    def test_skipped_and_logged_when_toggle_is_off(self):
        result = evaluate([plane(position=None)], count_no_position=False)
        self.assertEqual(result.in_zone, ())
        self.assertEqual(len(result.no_position_skipped), 1)

    def test_altitude_filter_still_applies(self):
        self.assertEqual(evaluate([plane(position=None, alt=20000)]).in_zone, ())

    def test_above_the_band_is_not_logged_as_skipped(self):
        result = evaluate([plane(position=None, alt=20000)], count_no_position=False)
        self.assertEqual(result.no_position_skipped, ())

    def test_ground_with_no_position_counts_when_on(self):
        self.assertEqual(len(evaluate([plane(position=None, alt=GROUND)]).in_zone), 1)

    def test_no_position_and_no_altitude_is_skipped_whatever_the_toggle(self):
        for toggle in (True, False):
            result = evaluate([plane(position=None, alt=None)], count_no_position=toggle)
            self.assertEqual(result.in_zone, ())
            self.assertEqual(result.no_position_skipped, ())


class ClosestTests(unittest.TestCase):
    def test_nearest_in_zone_aircraft(self):
        result = evaluate([plane("far", position=(40.15, -75.0)), plane("near", position=(40.05, -75.0))])
        self.assertEqual(result.closest.record.hex, "near")

    def test_a_positioned_aircraft_beats_a_position_less_one(self):
        result = evaluate([plane("nopos", position=None), plane("pos")])
        self.assertEqual(result.closest.record.hex, "pos")

    def test_falls_back_to_a_position_less_aircraft(self):
        self.assertEqual(evaluate([plane("nopos", position=None)]).closest.record.hex, "nopos")

    def test_no_aircraft_in_zone(self):
        self.assertIsNone(evaluate([plane(position=OUTSIDE)]).closest)

    def test_empty_list(self):
        result = evaluate([])
        self.assertEqual((result.aircraft, result.in_zone, result.closest), ((), (), None))


class FreshnessTests(unittest.TestCase):
    def test_never_succeeded_is_stale(self):
        self.assertFalse(data_is_fresh(100.0, None, False, 5.0))

    def test_within_max_age(self):
        self.assertTrue(data_is_fresh(100.0, 95.0, True, 5.0))

    def test_exactly_at_max_age_is_not_older_than_it(self):
        self.assertTrue(data_is_fresh(100.0, 95.0, False, 5.0))

    def test_older_than_max_age(self):
        self.assertFalse(data_is_fresh(100.1, 95.0, False, 5.0))

    def test_zero_means_stale_on_the_first_failed_poll(self):
        self.assertFalse(data_is_fresh(100.0, 99.9, True, 0))

    def test_zero_is_fresh_while_polls_succeed(self):
        self.assertTrue(data_is_fresh(100.0, 99.0, False, 0))


def inputs(enabled=True, center=True, schedule=True, fresh=True, aircraft=()):
    return Inputs(enabled, center, schedule, fresh, tuple(aircraft))


class EngineTestCase(unittest.TestCase):
    def assert_state(self, engine, state, reason):
        self.assertEqual((engine.state, engine.reason), (state, reason))


class EnteringBlackoutTests(EngineTestCase):
    def test_blackout_happens_on_the_same_poll(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs())
        self.assert_state(engine, CLEAR, REASON_CLEAR)
        transition = engine.update(1.0, inputs(aircraft=[plane()]))
        self.assert_state(engine, BLACKOUT, REASON_AIRCRAFT)
        self.assertEqual((transition.from_state, transition.to_state), (CLEAR, BLACKOUT))
        self.assertEqual(transition.reason, REASON_AIRCRAFT)
        self.assertEqual(transition.aircraft[0].hex, "a1")

    def test_stale_data_blacks_out_immediately(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs())
        engine.update(1.0, inputs(fresh=False))
        self.assert_state(engine, BLACKOUT, REASON_NO_DATA)

    def test_stale_data_outranks_aircraft_as_the_reason(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(fresh=False, aircraft=[plane()]))
        self.assert_state(engine, BLACKOUT, REASON_NO_DATA)

    def test_no_repeat_transition_while_nothing_changes(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(aircraft=[plane()]))
        self.assertIsNone(engine.update(1.0, inputs(aircraft=[plane()])))

    def test_reason_change_within_blackout_is_reported(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(aircraft=[plane()]))
        transition = engine.update(1.0, inputs(fresh=False))
        self.assertEqual((transition.from_state, transition.to_state), (BLACKOUT, BLACKOUT))
        self.assertEqual(transition.reason, REASON_NO_DATA)


class ReleaseDelayTests(EngineTestCase):
    def blacked_out(self, delay):
        engine = TriggerEngine(delay)
        engine.update(0.0, inputs(aircraft=[plane()]))
        return engine

    def test_held_until_the_delay_has_passed(self):
        engine = self.blacked_out(5.0)
        engine.update(1.0, inputs())
        engine.update(5.9, inputs())
        self.assertEqual(engine.state, BLACKOUT)
        transition = engine.update(6.0, inputs())
        self.assert_state(engine, CLEAR, REASON_CLEAR)
        self.assertEqual((transition.from_state, transition.to_state), (BLACKOUT, CLEAR))

    def test_reason_stays_put_while_holding(self):
        engine = self.blacked_out(5.0)
        self.assertIsNone(engine.update(1.0, inputs()))
        self.assertEqual(engine.reason, REASON_AIRCRAFT)

    def test_zero_releases_on_the_first_empty_poll(self):
        engine = self.blacked_out(0.0)
        engine.update(1.0, inputs())
        self.assertEqual(engine.state, CLEAR)

    def test_aircraft_returning_restarts_the_delay(self):
        engine = self.blacked_out(5.0)
        engine.update(1.0, inputs())
        engine.update(4.0, inputs(aircraft=[plane()]))
        engine.update(5.0, inputs())
        engine.update(9.9, inputs())
        self.assertEqual(engine.state, BLACKOUT)
        engine.update(10.0, inputs())
        self.assertEqual(engine.state, CLEAR)

    def test_stale_data_restarts_the_delay(self):
        engine = self.blacked_out(5.0)
        engine.update(1.0, inputs())
        engine.update(4.0, inputs(fresh=False))
        engine.update(5.0, inputs())
        engine.update(9.9, inputs())
        self.assertEqual(engine.state, BLACKOUT)
        engine.update(10.0, inputs())
        self.assertEqual(engine.state, CLEAR)

    def test_no_data_holds_until_data_returns_then_waits_the_delay(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(fresh=False))
        for now in range(1, 100):
            engine.update(float(now), inputs(fresh=False))
        self.assert_state(engine, BLACKOUT, REASON_NO_DATA)
        engine.update(100.0, inputs())
        self.assertEqual(engine.state, BLACKOUT)
        engine.update(105.0, inputs())
        self.assertEqual(engine.state, CLEAR)


class ScheduleTests(EngineTestCase):
    def test_outside_schedule_is_idle_with_aircraft_present(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(schedule=False, aircraft=[plane()]))
        self.assert_state(engine, IDLE, REASON_SCHEDULE)

    def test_outside_schedule_ignores_stale_data(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(schedule=False, fresh=False))
        self.assert_state(engine, IDLE, REASON_SCHEDULE)

    def test_schedule_start_with_aircraft_present_blacks_out_on_the_first_poll(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(schedule=False, aircraft=[plane()]))
        transition = engine.update(1.0, inputs(aircraft=[plane()]))
        self.assert_state(engine, BLACKOUT, REASON_AIRCRAFT)
        self.assertEqual(transition.from_state, IDLE)

    def test_schedule_start_with_an_empty_zone_goes_straight_to_clear(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(schedule=False))
        engine.update(1.0, inputs())
        self.assert_state(engine, CLEAR, REASON_CLEAR)

    def test_schedule_start_with_stale_data_fails_dark(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(schedule=False))
        engine.update(1.0, inputs(fresh=False))
        self.assert_state(engine, BLACKOUT, REASON_NO_DATA)

    def test_schedule_end_releases_at_once_without_the_delay(self):
        engine = TriggerEngine(60.0)
        engine.update(0.0, inputs(aircraft=[plane()]))
        engine.update(1.0, inputs(schedule=False, aircraft=[plane()]))
        self.assert_state(engine, IDLE, REASON_SCHEDULE)


class DisabledAndCenterTests(EngineTestCase):
    def test_disabled_is_idle_and_releases_a_blackout(self):
        engine = TriggerEngine(60.0)
        engine.update(0.0, inputs(aircraft=[plane()]))
        engine.update(1.0, inputs(enabled=False, aircraft=[plane()]))
        self.assert_state(engine, IDLE, REASON_DISABLED)

    def test_enabling_resumes_evaluation(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(enabled=False))
        engine.update(1.0, inputs(aircraft=[plane()]))
        self.assert_state(engine, BLACKOUT, REASON_AIRCRAFT)

    def test_no_center_is_idle_even_in_schedule(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(center=False))
        self.assert_state(engine, IDLE, REASON_NO_CENTER)

    def test_disabled_outranks_no_center_and_schedule(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(enabled=False, center=False, schedule=False))
        self.assert_state(engine, IDLE, REASON_DISABLED)


class TestTriggerTests(EngineTestCase):
    def test_forces_blackout_for_the_duration_then_resumes(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs())
        engine.start_test(1.0, 10)
        transition = engine.update(1.0, inputs())
        self.assert_state(engine, BLACKOUT, REASON_TEST)
        self.assertEqual(transition.reason, REASON_TEST)
        engine.update(10.9, inputs())
        self.assertEqual(engine.reason, REASON_TEST)
        engine.update(11.0, inputs())
        self.assert_state(engine, CLEAR, REASON_CLEAR)
        self.assertFalse(engine.test_active)

    def test_works_outside_the_schedule_and_returns_to_idle(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(schedule=False))
        engine.start_test(1.0, 3)
        engine.update(1.0, inputs(schedule=False))
        self.assert_state(engine, BLACKOUT, REASON_TEST)
        engine.update(4.0, inputs(schedule=False))
        self.assert_state(engine, IDLE, REASON_SCHEDULE)

    def test_works_before_the_center_is_set(self):
        engine = TriggerEngine(5.0)
        engine.start_test(0.0, 3)
        engine.update(0.0, inputs(center=False))
        self.assert_state(engine, BLACKOUT, REASON_TEST)
        engine.update(3.0, inputs(center=False))
        self.assert_state(engine, IDLE, REASON_NO_CENTER)

    def test_does_not_run_while_disabled_and_does_not_linger(self):
        engine = TriggerEngine(5.0)
        engine.start_test(0.0, 10)
        engine.update(0.0, inputs(enabled=False))
        self.assert_state(engine, IDLE, REASON_DISABLED)
        engine.update(1.0, inputs())
        self.assert_state(engine, CLEAR, REASON_CLEAR)

    def test_disabling_during_a_test_ends_it(self):
        engine = TriggerEngine(5.0)
        engine.start_test(0.0, 10)
        engine.update(0.0, inputs())
        engine.update(1.0, inputs(enabled=False))
        self.assert_state(engine, IDLE, REASON_DISABLED)
        self.assertFalse(engine.test_active)

    def test_aircraft_present_at_the_end_keeps_the_blackout(self):
        engine = TriggerEngine(5.0)
        engine.start_test(0.0, 3)
        engine.update(0.0, inputs())
        transition = engine.update(3.0, inputs(aircraft=[plane()]))
        self.assert_state(engine, BLACKOUT, REASON_AIRCRAFT)
        self.assertEqual(transition.from_state, BLACKOUT)

    def test_test_over_a_real_blackout_still_honors_the_release_delay(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs(aircraft=[plane()]))
        engine.start_test(1.0, 2)
        engine.update(1.0, inputs())  # aircraft left as the test began
        engine.update(3.0, inputs())  # test over, only 2 s clear
        self.assertEqual(engine.state, BLACKOUT)
        engine.update(6.0, inputs())
        self.assertEqual(engine.state, CLEAR)

    def test_aircraft_during_a_test_makes_the_release_delay_apply(self):
        engine = TriggerEngine(5.0)
        engine.update(0.0, inputs())
        engine.start_test(1.0, 3)
        engine.update(1.0, inputs())
        engine.update(2.0, inputs(aircraft=[plane()]))
        engine.update(3.0, inputs())
        engine.update(4.0, inputs())  # test over, only 1 s clear
        self.assertEqual(engine.state, BLACKOUT)
        engine.update(8.0, inputs())
        self.assertEqual(engine.state, CLEAR)

    def test_non_positive_duration_is_ignored(self):
        engine = TriggerEngine(5.0)
        engine.start_test(0.0, 0)
        engine.start_test(0.0, -5)
        self.assertFalse(engine.test_active)

    def test_starting_again_extends_the_test(self):
        engine = TriggerEngine(5.0)
        engine.start_test(0.0, 3)
        engine.update(0.0, inputs())
        engine.start_test(2.0, 3)
        engine.update(4.0, inputs())
        self.assertEqual(engine.reason, REASON_TEST)
        engine.update(5.0, inputs())
        self.assertEqual(engine.state, CLEAR)


class ResumeAfterCrashTests(EngineTestCase):
    def test_starts_blacked_out(self):
        self.assert_state(TriggerEngine(5.0, resume_blackout=True), BLACKOUT, REASON_RESUME)

    def test_empty_zone_must_last_the_full_delay_before_release(self):
        engine = TriggerEngine(5.0, resume_blackout=True)
        self.assertIsNone(engine.update(0.0, inputs()))
        engine.update(4.9, inputs())
        self.assert_state(engine, BLACKOUT, REASON_RESUME)
        engine.update(5.0, inputs())
        self.assert_state(engine, CLEAR, REASON_CLEAR)

    def test_aircraft_takes_over_the_reason(self):
        engine = TriggerEngine(5.0, resume_blackout=True)
        engine.update(0.0, inputs(aircraft=[plane()]))
        self.assert_state(engine, BLACKOUT, REASON_AIRCRAFT)

    def test_stale_data_holds_the_blackout(self):
        engine = TriggerEngine(5.0, resume_blackout=True)
        engine.update(0.0, inputs(fresh=False))
        self.assert_state(engine, BLACKOUT, REASON_NO_DATA)

    def test_outside_the_schedule_releases_to_idle(self):
        engine = TriggerEngine(5.0, resume_blackout=True)
        engine.update(0.0, inputs(schedule=False))
        self.assert_state(engine, IDLE, REASON_SCHEDULE)

    def test_disabled_releases_to_idle(self):
        engine = TriggerEngine(5.0, resume_blackout=True)
        engine.update(0.0, inputs(enabled=False))
        self.assert_state(engine, IDLE, REASON_DISABLED)

    def test_a_normal_start_is_idle(self):
        self.assertEqual(TriggerEngine(5.0).state, IDLE)


if __name__ == "__main__":
    unittest.main()
