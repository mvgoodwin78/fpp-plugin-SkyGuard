"""Schedule detection and the matrix message, over real HTTP against a fake FPP."""

import os
import tempfile
import unittest
import urllib.error
import urllib.request

from skyguard.config import Settings, parse_fixture
from skyguard.feeds.base import GROUND, AircraftRecord
from skyguard.fpp_api import FppApi, FppError, in_schedule_from_status, read_in_schedule
from skyguard.matrix import REFRESH_INTERVAL_S, MatrixController, fill_message
from skyguard.overlay import Ledger, resolve_fixtures, universe_entries
from skyguard.trigger import (
    BLACKOUT,
    CLEAR,
    IDLE,
    REASON_AIRCRAFT,
    REASON_NO_DATA,
    REASON_TEST,
    EvaluatedAircraft,
)
from tests.fake_fpp_server import FakeFpp


def plane(callsign="TEST1", alt=3000.4, gs=142.6, track=270.2, distance=3.0):
    record = AircraftRecord("abc123", callsign, 40.0, -75.0, alt, gs, track, 0.5, "receiver")
    return EvaluatedAircraft(record, distance, True)


class ScheduleTests(unittest.TestCase):
    def idle(self, **extra):
        return {"status": 0, "status_name": "idle", "scheduler": {"status": "idle"}, **extra}

    def test_idle(self):
        self.assertFalse(in_schedule_from_status(self.idle()))

    def test_playing(self):
        self.assertTrue(
            in_schedule_from_status({"status": 1, "status_name": "playing", "scheduler": {"status": "playing"}})
        )

    def test_scheduled_playlist_between_songs(self):
        self.assertTrue(in_schedule_from_status(self.idle(scheduler={"status": "playing"})))

    def test_started_by_hand(self):
        self.assertTrue(in_schedule_from_status(self.idle(scheduler={"status": "manual"})))

    def test_masters_background_status_with_code_zero(self):
        self.assertTrue(in_schedule_from_status(self.idle(status_name="playing background")))

    def test_stopping_gracefully_is_still_a_show(self):
        self.assertTrue(in_schedule_from_status({"status": 2, "status_name": "stopping gracefully"}))

    def test_a_non_zero_status_code_alone_is_a_show(self):
        self.assertTrue(in_schedule_from_status({"status": 1, "status_name": "idle"}))
        self.assertTrue(in_schedule_from_status({"status": "1", "status_name": "idle"}))

    def test_live_bridged_data_counts_as_a_show(self):
        self.assertTrue(in_schedule_from_status(self.idle(bridging=True)))
        self.assertTrue(in_schedule_from_status(self.idle(bridging=1)))

    def test_bridging_false_or_missing_does_not_count(self):
        self.assertFalse(in_schedule_from_status(self.idle(bridging=False)))
        self.assertFalse(in_schedule_from_status(self.idle(bridging=0)))
        self.assertFalse(in_schedule_from_status(self.idle()))

    def test_no_scheduler_block_as_in_bridge_mode(self):
        self.assertFalse(in_schedule_from_status({"status": 0, "status_name": "idle"}))

    def test_anything_unreadable_counts_as_a_show(self):
        for doc in (None, [], "idle", {}, {"fppd": "running"}):
            with self.subTest(doc=doc):
                self.assertTrue(in_schedule_from_status(doc))

    def test_unreadable_status_name_counts_as_a_show(self):
        self.assertTrue(in_schedule_from_status({"status": 0, "status_name": 5}))


class FppApiTests(unittest.TestCase):
    def setUp(self):
        self.fpp = FakeFpp()
        self.addCleanup(self.fpp.stop)
        self.api = FppApi(self.fpp.url, timeout_s=1.0)

    def test_status_is_read_from_fpps_real_route(self):
        self.assertFalse(read_in_schedule(self.api))
        self.assertEqual(self.fpp.requests[0][:2], ("GET", "/api/fppd/status"))

    def test_fpp_unreachable_counts_as_a_show(self):
        self.fpp.available = False
        self.assertTrue(read_in_schedule(self.api))

    def test_connection_refused_counts_as_a_show(self):
        self.assertTrue(read_in_schedule(FppApi("http://127.0.0.1:1", timeout_s=0.2)))

    def test_errors_carry_the_status(self):
        self.fpp.available = False
        with self.assertRaises(FppError) as caught:
            self.api.get_status()
        self.assertEqual(caught.exception.status, 503)

    def test_commands_post_a_json_array_of_strings(self):
        self.fpp.add_model("M")
        self.api.run_command("Overlay Model Effect", ["M", "False", "Text", 18])
        method, path, body = self.fpp.requests[0]
        self.assertEqual((method, path), ("POST", "/api/command/Overlay Model Effect"))
        self.assertEqual(body, ["M", "False", "Text", "18"])

    def test_model_names_with_spaces_are_encoded(self):
        self.fpp.add_model("Panel A", state=2)
        self.assertEqual(self.api.get_model("Panel A")["isActive"], 2)
        self.api.set_model_state("Panel A", 1)
        self.assertEqual(self.fpp.models["Panel A"]["state"], 1)

    def test_no_output_file_reads_as_none(self):
        self.fpp.universes_status = 404
        self.assertIsNone(self.api.get_universe_outputs())

    def test_other_output_read_failures_raise(self):
        self.fpp.universes_status = 500
        with self.assertRaises(FppError):
            self.api.get_universe_outputs()


class RealBoxRegressionTests(unittest.TestCase):
    """Behavior and data seen on a real FPP 10.3 Pi 4, which the fake must keep matching."""

    IDLE_STATUS = {
        "fppd": "running",
        "mode_name": "player",
        "status": 0,
        "status_name": "idle",
        "bridging": False,
        "scheduler": {"enabled": 1, "status": "idle"},
    }
    NO_UNIVERSES = {
        "channelOutputs": [
            {"type": "universes", "enabled": 0, "interface": "eth0", "threaded": 0,
             "startChannel": 1, "channelCount": -1, "universes": []}
        ],
        "status": "OK",
    }

    def setUp(self):
        self.fpp = FakeFpp()
        self.addCleanup(self.fpp.stop)
        self.api = FppApi(self.fpp.url, timeout_s=1.0)

    def test_gets_to_php_routes_send_no_json_header(self):
        # With the header, FPP answers 404 and every universe fixture looks unconfigured.
        self.fpp.universes = self.NO_UNIVERSES
        self.assertEqual(self.api.get_universe_outputs(), self.NO_UNIVERSES)

    def test_the_fake_reproduces_the_404(self):
        request = urllib.request.Request(
            self.fpp.url + "/api/channel/output/universeOutputs",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_json_bodies_still_carry_the_json_header(self):
        seen = {}
        real_route = self.fpp.route

        def spy(method, path, body):
            seen["body"] = body
            return real_route(method, path, body)

        self.fpp.route = spy
        self.api.set_ranges([5], 0)
        self.assertEqual(seen["body"], {"Value": 0})

    def test_settings_are_sent_as_a_bare_value(self):
        saved = {}
        self.fpp.on_setting = lambda name, value: saved.update({name: value})
        self.api.set_plugin_setting("enabled", "0")
        self.assertEqual(saved, {"enabled": 0})

    def test_a_real_lone_sequence_reads_as_a_show(self):
        # Only status_name shows this: status stays 0 and the scheduler says idle.
        playing = {
            **self.IDLE_STATUS,
            "status_name": "playing",
            "current_sequence": "Song.fseq",
            "current_playlist": {"count": "0", "playlist": ""},
        }
        self.assertTrue(in_schedule_from_status(playing))

    def test_a_real_idle_status_reads_as_no_show(self):
        self.assertFalse(in_schedule_from_status(self.IDLE_STATUS))

    def test_a_real_output_list_with_no_universes_gives_nothing(self):
        self.assertEqual(universe_entries(self.NO_UNIVERSES), [])

    def test_a_fixture_on_a_box_with_no_universes_is_flagged(self):
        result = resolve_fixtures((parse_fixture({"name": "A", "universe": 1, "dimmer_ch": 1}),), self.NO_UNIVERSES)
        self.assertEqual(result[0].problem, "Universe not found")


class FillMessageTests(unittest.TestCase):
    def fill(self, template, closest=None, count=1, unit="mi"):
        return fill_message(template, closest, count, unit)

    def test_all_placeholders(self):
        text = self.fill("{callsign} {altitude} {speed} {distance} {heading} {count}", plane(), count=2)
        self.assertEqual(text, "TEST1 3000 143 3.5 270 2")

    def test_callsign_falls_back_to_hex(self):
        self.assertEqual(self.fill("{callsign}", plane(callsign=None)), "abc123")

    def test_ground(self):
        self.assertEqual(self.fill("{altitude}", plane(alt=GROUND)), "ground")

    def test_distance_uses_the_users_unit_to_one_decimal(self):
        self.assertEqual(self.fill("{distance}", plane(distance=10.0), unit="nm"), "10.0")
        self.assertEqual(self.fill("{distance}", plane(distance=10.0), unit="km"), "18.5")

    def test_missing_values(self):
        closest = plane(alt=None, gs=None, track=None, distance=None)
        self.assertEqual(self.fill("{altitude} {speed} {heading} {distance}", closest), "-- -- -- --")

    def test_no_aircraft_at_all(self):
        self.assertEqual(self.fill("{callsign} {count}", None, count=0), "-- 0")

    def test_text_without_placeholders_is_unchanged(self):
        self.assertEqual(self.fill("Lights paused"), "Lights paused")

    def test_unknown_or_malformed_braces_are_left_alone(self):
        self.assertEqual(self.fill("{nope} {callsign", plane()), "{nope} {callsign")

    def test_a_format_attack_in_the_template_does_nothing(self):
        self.assertEqual(self.fill("{0.__class__}", plane()), "{0.__class__}")

    def test_control_characters_in_a_callsign_are_dropped(self):
        self.assertEqual(self.fill("{callsign}", plane(callsign="A\nB\x07C")), "ABC")

    def test_a_callsign_containing_a_placeholder_is_not_expanded(self):
        self.assertEqual(self.fill("{callsign}", plane(callsign="{count}"), count=5), "{count}")

    def test_the_default_messages_fill_correctly(self):
        defaults = Settings()
        self.assertEqual(
            self.fill(defaults.matrix_message, plane()),
            "Aircraft overhead: TEST1 at 3000 ft",
        )


def settings(**overrides):
    values = {
        "matrix_enabled": True,
        "matrix_models": ("Panel",),
        "matrix_message": "{callsign} {count}",
        "matrix_position": "Center",
    }
    values.update(overrides)
    return Settings(**values)


class MatrixTestCase(unittest.TestCase):
    def setUp(self):
        self.time = 100.0
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "active_ranges.json")
        self.fpp = FakeFpp()
        self.addCleanup(self.fpp.stop)
        self.fpp.add_model("Panel")
        self.api = FppApi(self.fpp.url, timeout_s=1.0)
        self.matrix = self.new_matrix()

    def new_matrix(self):
        self.ledger = Ledger(self.path)
        return MatrixController(self.api, self.ledger, lambda: self.time)

    def blackout(self, closest=None, count=1, reason=REASON_AIRCRAFT, **overrides):
        self.matrix.update(BLACKOUT, reason, closest, count, settings(**overrides))

    def clear(self, state=CLEAR, **overrides):
        self.matrix.update(state, "clear", None, 0, settings(**overrides))

    def panel(self):
        return self.fpp.models["Panel"]

    def texts(self):
        return [args for name, args in self.fpp.commands if args[2] == "Text"]


class MatrixShowTests(MatrixTestCase):
    def test_shows_the_filled_message(self):
        self.blackout(plane())
        self.assertEqual(self.panel()["effect"], "TEST1 1")
        self.assertEqual(self.panel()["state"], 1)

    def test_argument_order_matches_fpps_text_effect(self):
        self.blackout(
            plane(),
            matrix_color="#00FF00",
            matrix_font="Courier",
            matrix_size=24,
            matrix_position="Center",
            matrix_speed=30,
        )
        self.assertEqual(
            self.texts()[0],
            ["Panel", "False", "Text", "#00FF00", "Courier", "24", "false", "Center", "30", "0", "TEST1 1"],
        )

    def test_auto_enable_is_never_used_to_show_text(self):
        # On FPP it would make Center text stop after 25 ms.
        self.blackout(plane())
        self.assertNotIn("auto_enable_used", self.panel())

    def test_default_font_is_the_first_fpp_lists(self):
        self.blackout(plane())
        self.assertEqual(self.texts()[0][4], "Helvetica")

    def test_a_plain_font_is_preferred_when_fpp_lists_one(self):
        self.fpp.fonts = ["C059-BdIta", "FreeSans", "FreeSansBold"]
        self.blackout(plane())
        self.assertEqual(self.texts()[0][4], "FreeSansBold")

    def test_a_chosen_font_always_wins(self):
        self.fpp.fonts = ["C059-BdIta", "FreeSansBold"]
        self.blackout(plane(), matrix_font="C059-BdIta")
        self.assertEqual(self.texts()[0][4], "C059-BdIta")

    def test_multiple_models(self):
        self.fpp.add_model("Panel2")
        self.blackout(plane(), matrix_models=("Panel", "Panel2"))
        self.assertEqual(self.fpp.models["Panel2"]["effect"], "TEST1 1")

    def test_no_data_uses_its_own_message(self):
        self.blackout(reason=REASON_NO_DATA, matrix_nodata_message="Offline")
        self.assertEqual(self.panel()["effect"], "Offline")

    def test_empty_no_data_message_shows_nothing(self):
        self.blackout(reason=REASON_NO_DATA, matrix_nodata_message="")
        self.assertEqual(self.fpp.commands, [])

    def test_test_blackout_shows_the_message(self):
        self.blackout(reason=REASON_TEST, count=0)
        self.assertEqual(self.panel()["effect"], "-- 0")

    def test_disabled_skips_silently(self):
        self.blackout(plane(), matrix_enabled=False)
        self.assertEqual(self.fpp.requests, [])

    def test_no_models_skips_silently(self):
        self.blackout(plane(), matrix_models=())
        self.assertEqual(self.fpp.requests, [])

    def test_nothing_is_shown_outside_blackout(self):
        self.clear()
        self.clear(state=IDLE)
        self.assertEqual(self.fpp.requests, [])

    def test_release_delay_keeps_describing_the_last_aircraft(self):
        self.blackout(plane(), count=1)
        self.time += REFRESH_INTERVAL_S
        self.blackout(None, count=0)
        self.assertEqual(self.panel()["effect"], "TEST1 0")

    def test_the_last_aircraft_is_forgotten_after_the_blackout(self):
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.time += REFRESH_INTERVAL_S
        self.blackout(None, count=0, reason=REASON_TEST)
        self.assertEqual(self.panel()["effect"], "-- 0")


class MatrixRefreshTests(MatrixTestCase):
    def test_centered_text_updates_when_values_change(self):
        self.blackout(plane(), count=1)
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane(), count=2)
        self.assertEqual(self.panel()["effect"], "TEST1 2")

    def test_no_more_than_once_every_two_seconds(self):
        self.blackout(plane(), count=1)
        self.time += REFRESH_INTERVAL_S - 0.1
        self.blackout(plane(), count=2)
        self.assertEqual(self.panel()["effect"], "TEST1 1")
        self.assertEqual(len(self.texts()), 1)

    def test_unchanged_centered_text_is_not_resent(self):
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S * 5
        self.blackout(plane())
        self.assertEqual(len(self.texts()), 1)

    def test_a_scrolling_pass_is_not_restarted_while_running(self):
        self.blackout(plane(), count=1, matrix_position="Right to Left")
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane(), count=2, matrix_position="Right to Left")
        self.assertEqual(self.panel()["effect"], "TEST1 1")

    def test_a_finished_scroll_shows_the_latest_text(self):
        self.blackout(plane(), count=1, matrix_position="Right to Left")
        self.panel()["effect"] = None  # the pass finished
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane(), count=2, matrix_position="Right to Left")
        self.assertEqual(self.panel()["effect"], "TEST1 2")

    def test_a_finished_scroll_repeats_unchanged_text(self):
        self.blackout(plane(), matrix_position="Right to Left")
        self.panel()["effect"] = None
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane(), matrix_position="Right to Left")
        self.assertEqual(len(self.texts()), 2)


class MatrixClearTests(MatrixTestCase):
    def test_a_model_we_enabled_disables_itself_when_stopped(self):
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.panel()["state"], 0)
        self.assertEqual(self.fpp.commands[-1][1], ["Panel", "Enabled", "Stop Effects"])

    def test_no_state_call_follows_the_stop(self):
        # On current FPP that would cancel the pending stop.
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S
        self.fpp.requests.clear()
        self.clear()
        self.assertFalse(any(path.endswith("/state") for _m, path, _b in self.fpp.requests))

    def test_a_model_the_user_had_enabled_stays_enabled(self):
        self.panel()["state"] = 1
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.assertEqual(self.panel()["state"], 1)
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.fpp.commands[-1][1], ["Panel", "False", "Stop Effects"])

    def test_a_transparent_model_keeps_its_state(self):
        self.panel()["state"] = 2
        self.blackout(plane())
        self.assertEqual(self.panel()["state"], 2)
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.assertEqual(self.panel()["state"], 2)

    def test_a_second_blackout_records_the_state_afresh(self):
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.panel()["state"] = 1
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane())
        self.assertEqual(self.ledger.matrix["Panel"], 1)

    def test_turning_the_matrix_off_during_a_blackout_clears_it(self):
        self.blackout(plane())
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane(), matrix_enabled=False)
        self.assertIsNone(self.panel()["effect"])

    def test_a_model_removed_from_settings_is_cleared(self):
        self.fpp.add_model("Panel2")
        self.blackout(plane(), matrix_models=("Panel", "Panel2"))
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane(), matrix_models=("Panel",))
        self.assertIsNone(self.fpp.models["Panel2"]["effect"])
        self.assertEqual(self.fpp.models["Panel2"]["state"], 0)

    def test_clear_all_ignores_the_rate_limit_for_shutdown(self):
        self.blackout(plane())
        self.matrix.clear_all()
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.ledger.matrix, {})

    def test_failed_clear_is_retried(self):
        self.blackout(plane())
        self.fpp.fail_commands = True
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.assertEqual(self.ledger.matrix, {"Panel": 0})
        self.fpp.fail_commands = False
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.assertEqual(self.ledger.matrix, {})
        self.assertIsNone(self.panel()["effect"])

    def test_a_deleted_model_is_forgotten(self):
        self.blackout(plane())
        del self.fpp.models["Panel"]
        self.time += REFRESH_INTERVAL_S
        with self.assertLogs("skyguard", level="WARNING"):
            self.clear()
        self.assertEqual(self.ledger.matrix, {})


class MatrixRetryTests(MatrixTestCase):
    def test_a_failed_enable_is_retried_before_sending_text(self):
        self.fpp.fail_model_state = True
        with self.assertLogs("skyguard", level="WARNING"):
            self.blackout(plane())
        self.assertEqual(self.texts(), [])
        self.fpp.fail_model_state = False
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane())
        self.assertEqual(self.panel()["state"], 1)
        self.assertEqual(self.panel()["effect"], "TEST1 1")

    def test_the_original_state_survives_a_failed_enable(self):
        self.fpp.fail_model_state = True
        with self.assertLogs("skyguard", level="WARNING"):
            self.blackout(plane())
        self.fpp.fail_model_state = False
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane())
        self.assertEqual(self.ledger.matrix["Panel"], 0)

    def test_a_server_error_on_clear_does_not_forget_a_model_that_exists(self):
        self.blackout(plane())
        self.fpp.fail_commands = True
        self.fpp.command_error = 500
        self.time += REFRESH_INTERVAL_S
        with self.assertLogs("skyguard", level="WARNING"):
            self.clear()
        self.assertEqual(self.ledger.matrix, {"Panel": 0})
        self.fpp.fail_commands = False
        self.time += REFRESH_INTERVAL_S
        self.clear()
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.ledger.matrix, {})

    def test_fpp_being_down_on_clear_does_not_forget_the_model(self):
        self.blackout(plane())
        self.fpp.available = False
        self.time += REFRESH_INTERVAL_S
        with self.assertLogs("skyguard", level="WARNING"):
            self.clear()
        self.assertEqual(self.ledger.matrix, {"Panel": 0})


class MatrixRestartTests(MatrixTestCase):
    def test_a_restarted_daemon_still_restores_the_model(self):
        self.blackout(plane())
        self.matrix = self.new_matrix()
        self.clear()
        self.assertIsNone(self.panel()["effect"])
        self.assertEqual(self.panel()["state"], 0)

    def test_the_original_state_survives_a_restart_during_a_blackout(self):
        self.blackout(plane())  # recorded as disabled, now enabled by us
        self.matrix = self.new_matrix()
        self.blackout(plane())
        self.assertEqual(self.ledger.matrix["Panel"], 0)

    def test_prior_state_is_recorded_before_the_model_changes(self):
        seen = []
        real = self.api.set_model_state

        def spy(name, state):
            seen.append(dict(Ledger(self.path).matrix))
            real(name, state)

        self.api.set_model_state = spy
        self.blackout(plane())
        self.assertEqual(seen, [{"Panel": 0}])


class MatrixFailureTests(MatrixTestCase):
    def test_a_missing_model_logs_once_and_does_not_raise(self):
        with self.assertLogs("skyguard", level="WARNING") as captured:
            for _ in range(5):
                self.blackout(plane(), matrix_models=("Gone",))
                self.time += REFRESH_INTERVAL_S
        self.assertEqual(sum("Gone" in line for line in captured.output), 1)

    def test_one_bad_model_does_not_stop_another(self):
        with self.assertLogs("skyguard", level="WARNING"):
            self.blackout(plane(), matrix_models=("Gone", "Panel"))
        self.assertEqual(self.panel()["effect"], "TEST1 1")

    def test_fpp_down_does_not_raise(self):
        self.fpp.available = False
        with self.assertLogs("skyguard", level="WARNING"):
            self.blackout(plane())

    def test_no_fonts_does_not_raise(self):
        self.fpp.fonts = []
        with self.assertLogs("skyguard", level="WARNING"):
            self.blackout(plane())
        self.assertIsNone(self.panel()["effect"])

    def test_a_failed_show_retries_at_the_rate_limit(self):
        self.fpp.fail_commands = True
        with self.assertLogs("skyguard", level="WARNING"):
            self.blackout(plane())
        self.fpp.fail_commands = False
        self.blackout(plane())
        self.assertIsNone(self.panel()["effect"])
        self.time += REFRESH_INTERVAL_S
        self.blackout(plane())
        self.assertEqual(self.panel()["effect"], "TEST1 1")


if __name__ == "__main__":
    unittest.main()
