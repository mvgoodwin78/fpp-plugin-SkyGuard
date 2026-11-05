"""Channel ranges, the ledger and fixture resolution, over real HTTP against a fake FPP."""

import json
import os
import tempfile
import unittest

from skyguard.config import parse_fixture
from skyguard.fpp_api import FppApi
from skyguard.notifier import Notifier, WarningSink
from skyguard.overlay import (
    HOLD_RESEND_S,
    MAX_CHANNEL,
    RESOLVE_INTERVAL_S,
    ChannelResolver,
    Ledger,
    OverlayController,
    blackout_target,
    resolve_fixtures,
    universe_entries,
)
from tests.fake_fpp_server import FakeFpp


def universes(*entries, enabled=1):
    return {"channelOutputs": [{"type": "universes", "enabled": enabled, "universes": list(entries)}]}


def universe(id, start, count=512, **extra):
    return {"active": 1, "id": id, "startChannel": start, "channelCount": count, **extra}


def fixture(name="H", **fields):
    return parse_fixture({"name": name, **fields})


class Rig:
    def __init__(self, test, delete_bug=True):
        self.time = 0.0
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "active_ranges.json")
        self.fpp = FakeFpp(delete_bug=delete_bug)
        test.addCleanup(self.fpp.stop)
        self.api = FppApi(self.fpp.url, timeout_s=1.0)
        self.warnings = WarningSink()
        self.notifier = Notifier([self.warnings])
        self.new_controller()

    def clock(self):
        return self.time

    def new_controller(self):
        self.ledger = Ledger(self.path)
        self.controller = OverlayController(self.api, self.ledger, self.notifier, self.clock)
        return self.controller

    def saved(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)["channels"]


class BlackoutTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)

    def test_forces_only_the_listed_channels_to_their_values(self):
        self.r.controller.sync({10: 0, 11: 8})
        self.assertEqual(self.r.fpp.output(10), 0)
        self.assertEqual(self.r.fpp.output(11), 8)
        self.assertEqual(self.r.fpp.output(12), 100)  # pan/tilt/color still follow the sequence

    def test_zero_is_a_real_off_value(self):
        self.r.controller.sync({10: 0})
        self.assertEqual(self.r.fpp.ranges[10], 0)

    def test_wire_format_matches_fpps_documented_api(self):
        self.r.controller.sync({10: 0, 11: 0})
        method, path, body = self.r.fpp.requests[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/api/overlays/range/10-10,11-11")
        self.assertEqual(body, {"Value": 0})

    def test_channels_with_the_same_value_share_a_request(self):
        self.r.controller.sync({c: 0 for c in range(1, 51)})
        self.assertEqual(len(self.r.fpp.requests), 1)

    def test_large_shows_are_split_into_bounded_requests(self):
        self.r.controller.sync({c: 0 for c in range(1, 251)})
        self.assertEqual(len(self.r.fpp.requests), 3)
        self.assertEqual(len(self.r.fpp.ranges), 250)

    def test_nothing_is_resent_before_the_interval(self):
        self.r.controller.sync({10: 0})
        count = len(self.r.fpp.requests)
        self.r.time += HOLD_RESEND_S - 0.1
        self.r.controller.sync({10: 0})
        self.assertEqual(len(self.r.fpp.requests), count)

    def test_held_ranges_are_resent_at_the_interval(self):
        self.r.controller.sync({10: 0})
        count = len(self.r.fpp.requests)
        self.r.time += HOLD_RESEND_S
        self.r.controller.sync({10: 0})
        self.assertEqual(len(self.r.fpp.requests), count + 1)

    def test_resending_restores_ranges_after_an_fppd_restart(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.restart_fppd()
        self.assertEqual(self.r.fpp.output(10), 100)
        self.r.time += HOLD_RESEND_S
        self.r.controller.sync({10: 0})
        self.assertEqual(self.r.fpp.output(10), 0)

    def test_a_new_channel_is_sent_at_once(self):
        self.r.controller.sync({10: 0})
        self.r.controller.sync({10: 0, 11: 0})
        self.assertEqual(self.r.fpp.output(11), 0)

    def test_a_changed_value_is_sent_at_once(self):
        self.r.controller.sync({10: 0})
        self.r.controller.sync({10: 8})
        self.assertEqual(self.r.fpp.output(10), 8)

    def test_a_failed_set_is_retried_on_the_next_sync(self):
        self.r.fpp.fail_set = True
        self.r.controller.sync({10: 0})
        self.r.fpp.fail_set = False
        self.r.controller.sync({10: 0})
        self.assertEqual(self.r.fpp.output(10), 0)


class ReleaseSafetyTests(unittest.TestCase):
    """The reason set-then-delete exists: on 9.5-10.2 a bare delete means full on."""

    def setUp(self):
        self.r = Rig(self, delete_bug=True)

    def test_the_fake_reproduces_the_bug(self):
        self.r.api.delete_ranges([10])
        self.assertEqual(self.r.fpp.output(10), 255)

    def test_a_normal_release_passes_the_sequence_through(self):
        self.r.controller.sync({10: 0})
        self.r.controller.release_all()
        self.assertEqual(self.r.fpp.output(10), 100)
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_release_sets_before_deleting(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.requests.clear()
        self.r.controller.release_all()
        bodies = [body for _method, _path, body in self.r.fpp.requests]
        self.assertEqual(bodies, [{"Value": 0}, {"delete": True}])

    def test_release_after_an_fppd_restart_is_never_full_on(self):
        self.r.controller.sync({10: 0, 11: 8})
        self.r.fpp.restart_fppd()
        self.r.controller.release_all()
        self.assertEqual(self.r.fpp.ever_full_on, [])
        self.assertEqual(self.r.fpp.output(10), 100)

    def test_a_failed_set_never_goes_on_to_delete(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.restart_fppd()
        self.r.fpp.fail_set = True
        self.r.fpp.requests.clear()
        self.r.controller.release_all()
        self.assertFalse(any(b == {"delete": True} for _m, _p, b in self.r.fpp.requests))
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_release_after_a_daemon_restart_is_never_full_on(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.restart_fppd()
        self.r.new_controller().release_all()
        self.assertEqual(self.r.fpp.ever_full_on, [])

    def test_never_uses_delete_all(self):
        self.r.controller.sync({10: 0})
        self.r.controller.release_all()
        self.assertFalse(any("deleteAll" in (b or {}) for _m, _p, b in self.r.fpp.requests))

    def test_a_channel_leaving_the_target_is_released_while_others_hold(self):
        self.r.controller.sync({10: 0, 11: 0})
        self.r.controller.sync({11: 0})
        self.assertEqual(self.r.fpp.output(10), 100)
        self.assertEqual(self.r.fpp.output(11), 0)

    def test_a_failed_delete_stays_dark_and_retries(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.fail_delete = True
        self.assertFalse(self.r.controller.release_all())
        self.assertEqual(self.r.fpp.output(10), 0)
        self.assertIn(10, self.r.controller.held)
        self.r.fpp.fail_delete = False
        self.assertTrue(self.r.controller.release_all())
        self.assertEqual(self.r.fpp.output(10), 100)
        self.assertEqual(self.r.controller.held, {})

    def test_releasing_nothing_sends_nothing(self):
        self.r.controller.release_all()
        self.assertEqual(self.r.fpp.requests, [])

    def test_the_fixed_fpp_behaves_the_same(self):
        r = Rig(self, delete_bug=False)
        r.controller.sync({10: 0})
        r.controller.release_all()
        self.assertEqual(r.fpp.output(10), 100)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)

    def test_channel_is_recorded_before_its_range_is_set(self):
        seen = []
        real = self.r.api.set_ranges

        def spy(channels, value):
            seen.append(self.r.saved())
            real(channels, value)

        self.r.api.set_ranges = spy
        self.r.controller.sync({10: 0})
        self.assertEqual(seen, [{"10": 0}])

    def test_channel_is_recorded_even_if_the_set_fails(self):
        self.r.fpp.fail_set = True
        self.r.controller.sync({10: 0})
        self.assertEqual(self.r.saved(), {"10": 0})

    def test_channel_is_forgotten_only_after_the_delete_succeeds(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.fail_delete = True
        self.r.controller.release_all()
        self.assertEqual(self.r.saved(), {"10": 0})
        self.r.fpp.fail_delete = False
        self.r.controller.release_all()
        self.assertEqual(self.r.saved(), {})

    def test_a_restart_with_held_channels_resumes_the_blackout(self):
        self.r.controller.sync({10: 0})
        self.assertTrue(Ledger(self.r.path).resume_blackout)

    def test_a_restart_with_nothing_held_does_not_resume(self):
        self.r.controller.sync({10: 0})
        self.r.controller.release_all()
        self.assertFalse(Ledger(self.r.path).resume_blackout)

    def test_a_first_start_with_no_file_does_not_resume_or_warn(self):
        ledger = Ledger(os.path.join(self.r.dir.name, "none.json"))
        self.assertFalse(ledger.resume_blackout)
        self.assertFalse(ledger.corrupt)

    def test_a_fixture_removed_while_down_is_still_released(self):
        self.r.controller.sync({10: 0, 11: 8})
        controller = self.r.new_controller()
        self.assertEqual(controller.held, {10: 0, 11: 8})
        controller.sync({11: 8})  # fixture owning channel 10 is gone
        self.assertEqual(self.r.fpp.output(10), 100)
        self.assertEqual(self.r.fpp.output(11), 8)

    def test_a_write_failure_does_not_stop_the_blackout(self):
        self.r.ledger.save = lambda: False
        self.r.controller.sync({10: 0})
        self.assertEqual(self.r.fpp.output(10), 0)

    def test_corrupt_files_resume_the_blackout_and_are_flagged(self):
        for text in ("{not json", "[]", '{"channels": 5}', '{"channels": {"a": 1}, "matrix": {}}'):
            with self.subTest(text=text):
                with open(self.r.path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                with self.assertLogs("skyguard", level="WARNING"):
                    ledger = Ledger(self.r.path)
                self.assertTrue(ledger.corrupt)
                self.assertTrue(ledger.resume_blackout)

    def test_corrupt_ledger_adopts_current_channels_and_releases_them(self):
        with open(self.r.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertLogs("skyguard", level="WARNING"):
            controller = self.r.new_controller()
            controller.adopt_after_corrupt_ledger({10: 0})
        self.assertFalse(self.r.ledger.corrupt)
        controller.release_all()
        self.assertEqual(self.r.fpp.ever_full_on, [])
        self.assertEqual(self.r.fpp.output(10), 100)

    def test_adopting_does_nothing_with_a_good_ledger(self):
        self.r.controller.adopt_after_corrupt_ledger({10: 0})
        self.assertEqual(self.r.controller.held, {})

    def test_save_leaves_no_temp_files_behind(self):
        self.r.controller.sync({10: 0})
        self.r.controller.release_all()
        self.assertEqual(os.listdir(self.r.dir.name), ["active_ranges.json"])


class FailureWarningTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)

    def test_failing_to_black_out_raises_a_warning(self):
        self.r.fpp.available = False
        self.assertFalse(self.r.controller.sync({10: 0}))
        self.assertEqual(len(self.r.warnings.warnings()), 1)
        self.assertIn("black", self.r.warnings.warnings()[0])

    def test_warning_clears_when_it_works_again(self):
        self.r.fpp.available = False
        self.r.controller.sync({10: 0})
        self.r.fpp.available = True
        self.assertTrue(self.r.controller.sync({10: 0}))
        self.assertEqual(self.r.warnings.warnings(), [])

    def test_failing_to_release_raises_a_warning(self):
        self.r.controller.sync({10: 0})
        self.r.fpp.fail_delete = True
        self.r.controller.release_all()
        self.assertEqual(len(self.r.warnings.warnings()), 1)

    def test_no_warning_when_all_is_well(self):
        self.r.controller.sync({10: 0})
        self.assertEqual(self.r.warnings.warnings(), [])


class UniverseEntryTests(unittest.TestCase):
    def test_active_universes_on_enabled_outputs(self):
        self.assertEqual(universe_entries(universes(universe(1, 1))), [(1, 1, 1, 512)])

    def test_universe_count_spans_a_range(self):
        doc = universes(universe(10, 1001, universeCount=4))
        self.assertEqual(universe_entries(doc), [(10, 13, 1001, 512)])

    def test_inactive_universe_and_disabled_output_are_skipped(self):
        self.assertEqual(universe_entries(universes(universe(1, 1, active=0))), [])
        self.assertEqual(universe_entries(universes(universe(1, 1), enabled=0)), [])

    def test_missing_enabled_means_disabled_as_in_fppd(self):
        self.assertEqual(universe_entries({"channelOutputs": [{"universes": [universe(1, 1)]}]}), [])

    def test_malformed_documents_give_nothing(self):
        for doc in (None, [], {}, {"channelOutputs": 5}, {"channelOutputs": [5]}):
            self.assertEqual(universe_entries(doc), [])

    def test_malformed_entries_are_skipped_without_losing_others(self):
        doc = universes({"id": "x", "startChannel": 1, "channelCount": 512}, {"id": 2}, 5, universe(3, 9))
        self.assertEqual(universe_entries(doc), [(3, 3, 9, 512)])


class ResolveTests(unittest.TestCase):
    def channels(self, row, doc):
        return resolve_fixtures((row,), doc)[0]

    def test_spec_formula(self):
        doc = universes(universe(3, 1025))
        result = self.channels(fixture(universe=3, dimmer_ch=10, shutter_ch=11, shutter_off=8), doc)
        self.assertTrue(result.valid)
        self.assertEqual(result.channels, ((1034, 0, "dimmer"), (1035, 8, "shutter")))

    def test_dmx_channel_1_is_the_universe_start(self):
        self.assertEqual(self.channels(fixture(universe=1, dimmer_ch=1), universes(universe(1, 1))).channels[0][0], 1)

    def test_universe_inside_a_multi_universe_entry(self):
        doc = universes(universe(10, 1001, universeCount=4))
        self.assertEqual(self.channels(fixture(universe=12, dimmer_ch=5), doc).channels[0][0], 1001 + 2 * 512 + 4)

    def test_channel_count_other_than_512(self):
        doc = universes(universe(10, 1, count=510, universeCount=2))
        self.assertEqual(self.channels(fixture(universe=11, dimmer_ch=1), doc).channels[0][0], 511)

    def test_two_universes_resolve_independently(self):
        doc = universes(universe(1, 1), universe(2, 513))
        rows = (fixture("A", universe=1, dimmer_ch=7), fixture("B", universe=2, dimmer_ch=7))
        self.assertEqual(blackout_target(resolve_fixtures(rows, doc)), {7: 0, 519: 0})

    def test_universe_not_found(self):
        result = self.channels(fixture(universe=9, dimmer_ch=1), universes(universe(1, 1)))
        self.assertFalse(result.valid)
        self.assertEqual(result.problem, "Universe not found")
        self.assertEqual(result.channels, ())

    def test_no_output_configuration_at_all(self):
        self.assertFalse(self.channels(fixture(universe=1, dimmer_ch=1), None).valid)

    def test_duplicate_universe_uses_the_first_and_logs(self):
        doc = universes(universe(1, 1), universe(1, 5001))
        with self.assertLogs("skyguard", level="WARNING"):
            result = self.channels(fixture(universe=1, dimmer_ch=1), doc)
        self.assertEqual(result.channels[0][0], 1)

    def test_channel_past_the_end_of_its_universe_never_lands_on_the_next(self):
        doc = universes(universe(1, 1, count=100), universe(2, 101))
        result = self.channels(fixture(universe=1, dimmer_ch=101), doc)
        self.assertFalse(result.valid)
        self.assertEqual(result.channels, ())

    def test_the_good_channel_still_works_when_the_other_is_out_of_range(self):
        doc = universes(universe(1, 1, count=100))
        result = self.channels(fixture(universe=1, dimmer_ch=5, shutter_ch=200), doc)
        self.assertFalse(result.valid)
        self.assertEqual(result.channels, ((5, 0, "dimmer"),))

    def test_start_channel_needs_no_universe_configuration(self):
        result = self.channels(fixture(start_channel=5001, dimmer_ch=1, shutter_ch=2), None)
        self.assertTrue(result.valid)
        self.assertEqual([c[0] for c in result.channels], [5001, 5002])

    def test_start_channel_past_fppds_limit_is_refused(self):
        result = self.channels(fixture(start_channel=MAX_CHANNEL, dimmer_ch=2), None)
        self.assertFalse(result.valid)
        self.assertEqual(result.channels, ())
        self.assertTrue(self.channels(fixture(start_channel=MAX_CHANNEL, dimmer_ch=1), None).valid)

    def test_incomplete_and_disabled_rows_resolve_to_nothing(self):
        rows = (
            parse_fixture({"name": "Half", "universe": 1}),
            parse_fixture({"name": "Off", "universe": 1, "dimmer_ch": 1, "enabled": False}),
        )
        result = resolve_fixtures(rows, universes(universe(1, 1)))
        self.assertEqual(blackout_target(result), {})
        self.assertEqual(result[0].problem, "Incomplete")
        self.assertTrue(result[1].valid)

    def test_shared_channel_with_the_same_value_is_fine(self):
        rows = (fixture("A", universe=1, dimmer_ch=1), fixture("B", universe=1, dimmer_ch=1))
        result = resolve_fixtures(rows, universes(universe(1, 1)))
        self.assertTrue(all(r.valid for r in result))

    def test_shared_channel_with_a_different_value_flags_the_second_and_keeps_the_first(self):
        rows = (
            fixture("A", universe=1, dimmer_ch=1, dimmer_off=0),
            fixture("B", universe=1, dimmer_ch=1, dimmer_off=9, shutter_ch=2),
        )
        result = resolve_fixtures(rows, universes(universe(1, 1)))
        self.assertTrue(result[0].valid)
        self.assertFalse(result[1].valid)
        self.assertEqual(blackout_target(result), {1: 0, 2: 0})


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig(self)
        self.r.fpp.universes = universes(universe(1, 1))
        self.resolver = ChannelResolver(self.r.api, self.r.notifier, self.r.clock)
        self.rows = (fixture("A", universe=1, dimmer_ch=5),)

    def reads(self):
        return sum(1 for _m, path, _b in self.r.fpp.requests if "universeOutputs" in path)

    def test_reads_fpps_real_route(self):
        result = self.resolver.refresh(self.rows)
        self.assertEqual(result[0].channels[0][0], 5)
        self.assertEqual(self.r.fpp.requests[0][1], "/api/channel/output/universeOutputs")

    def test_output_configuration_is_reread_periodically_not_every_loop(self):
        self.resolver.refresh(self.rows)
        self.r.time += RESOLVE_INTERVAL_S - 1
        self.resolver.refresh(self.rows)
        self.assertEqual(self.reads(), 1)
        self.r.time += 1
        self.resolver.refresh(self.rows)
        self.assertEqual(self.reads(), 2)

    def test_picks_up_a_changed_output(self):
        self.resolver.refresh(self.rows)
        self.r.fpp.universes = universes(universe(1, 1001))
        self.r.time += RESOLVE_INTERVAL_S
        self.assertEqual(self.resolver.refresh(self.rows)[0].channels[0][0], 1005)

    def test_picks_up_changed_fixtures_at_once(self):
        self.resolver.refresh(self.rows)
        result = self.resolver.refresh((fixture("A", universe=1, dimmer_ch=9),))
        self.assertEqual(result[0].channels[0][0], 9)
        self.assertEqual(self.reads(), 1)

    def test_missing_universe_warns_and_recovers(self):
        rows = (fixture("A", universe=7, dimmer_ch=1),)
        self.resolver.refresh(rows)
        self.assertEqual(len(self.r.warnings.warnings()), 1)
        self.assertIn("'A'", self.r.warnings.warnings()[0])
        self.r.fpp.universes = universes(universe(7, 1))
        self.r.time += RESOLVE_INTERVAL_S
        self.resolver.refresh(rows)
        self.assertEqual(self.r.warnings.warnings(), [])

    def test_removing_an_invalid_fixture_clears_its_warning(self):
        self.resolver.refresh((fixture("A", universe=7, dimmer_ch=1),))
        self.resolver.refresh(())
        self.assertEqual(self.r.warnings.warnings(), [])

    def test_incomplete_rows_do_not_warn(self):
        self.resolver.refresh((parse_fixture({"name": "Half"}),))
        self.assertEqual(self.r.warnings.warnings(), [])

    def test_fpp_going_away_keeps_the_last_good_resolution(self):
        self.resolver.refresh(self.rows)
        self.r.fpp.available = False
        self.r.time += RESOLVE_INTERVAL_S
        result = self.resolver.refresh(self.rows)
        self.assertEqual(result[0].channels[0][0], 5)
        self.assertEqual(self.r.warnings.warnings(), [])

    def test_fpp_unavailable_at_startup_retries_on_the_next_loop(self):
        self.r.fpp.available = False
        with self.assertLogs("skyguard", level="WARNING"):
            self.resolver.refresh(self.rows)
        self.assertFalse(self.resolver.resolved[0].valid)
        self.r.fpp.available = True
        self.assertTrue(self.resolver.refresh(self.rows)[0].valid)
        self.assertEqual(self.r.warnings.warnings(), [])

    def test_repeated_read_failures_log_once(self):
        self.r.fpp.available = False
        with self.assertLogs("skyguard", level="WARNING") as captured:
            for _ in range(5):
                self.resolver.refresh(self.rows)
        self.assertEqual(sum("channel outputs" in line for line in captured.output), 1)

    def test_no_output_file_is_handled(self):
        self.r.fpp.universes_status = 404
        self.resolver.refresh(self.rows)
        self.assertFalse(self.resolver.resolved[0].valid)


if __name__ == "__main__":
    unittest.main()
