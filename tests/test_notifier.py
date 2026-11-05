import logging
import threading
import unittest

from skyguard.notifier import LogSink, Notifier, WarningSink


class RecordingSink:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


class BrokenSink:
    def handle(self, event):
        raise RuntimeError("sink down")


class DispatchTests(unittest.TestCase):
    def test_every_sink_receives_the_event(self):
        first, second = RecordingSink(), RecordingSink()
        Notifier([first, second]).emit("blackout_started", {"reason": "aircraft"})
        self.assertEqual(first.events[0].name, "blackout_started")
        self.assertEqual(second.events[0].details, {"reason": "aircraft"})

    def test_unknown_event_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            Notifier().emit("not_an_event")

    def test_a_failing_sink_does_not_stop_the_others(self):
        good = RecordingSink()
        notifier = Notifier([BrokenSink(), good])
        with self.assertLogs("skyguard", level="ERROR"):
            self.assertTrue(notifier.emit("blackout_started"))
        self.assertEqual(len(good.events), 1)


class ThreadSafetyTests(unittest.TestCase):
    def test_emitting_is_serialized_across_threads(self):
        notifier = Notifier()
        results = []

        def try_lock():
            acquired = notifier._lock.acquire(blocking=False)
            results.append(acquired)
            if acquired:
                notifier._lock.release()

        class ProbeSink:
            def handle(self, event):
                worker = threading.Thread(target=try_lock)
                worker.start()
                worker.join()

        notifier.sinks.append(ProbeSink())
        notifier.emit("blackout_started")
        self.assertEqual(results, [False])


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.sink = RecordingSink()
        self.notifier = Notifier([self.sink])

    def names(self):
        return [event.name for event in self.sink.events]

    def test_one_warning_per_outage(self):
        self.assertTrue(self.notifier.emit("all_feeds_down"))
        for _ in range(100):
            self.assertFalse(self.notifier.emit("all_feeds_down"))
        self.assertEqual(self.names(), ["all_feeds_down"])

    def test_a_new_outage_warns_again(self):
        self.notifier.emit("all_feeds_down")
        self.notifier.emit("feeds_restored")
        self.notifier.emit("all_feeds_down")
        self.assertEqual(self.names(), ["all_feeds_down", "feeds_restored", "all_feeds_down"])

    def test_clearing_with_nothing_active_is_suppressed(self):
        self.assertFalse(self.notifier.emit("feeds_restored"))
        self.assertFalse(self.notifier.emit("receiver_restored"))
        self.assertEqual(self.names(), [])

    def test_blackout_events_are_never_suppressed(self):
        self.notifier.emit("blackout_started")
        self.notifier.emit("blackout_cleared")
        self.notifier.emit("blackout_started")
        self.assertEqual(len(self.sink.events), 3)

    def test_each_fixture_warns_separately(self):
        self.assertTrue(self.notifier.emit("fixture_invalid", {"fixture": "A"}))
        self.assertTrue(self.notifier.emit("fixture_invalid", {"fixture": "B"}))
        self.assertFalse(self.notifier.emit("fixture_invalid", {"fixture": "A"}))


class WarningSinkTests(unittest.TestCase):
    def setUp(self):
        self.warnings = WarningSink()
        self.notifier = Notifier([self.warnings])

    def test_receiver_restored_clears_both_receiver_warnings(self):
        self.notifier.emit("receiver_lost")
        self.notifier.emit("fallback_active")
        self.assertEqual(len(self.warnings.warnings()), 2)
        self.notifier.emit("receiver_restored")
        self.assertEqual(self.warnings.warnings(), [])

    def test_all_feeds_down_and_restored(self):
        self.notifier.emit("all_feeds_down")
        self.assertEqual(len(self.warnings.warnings()), 1)
        self.notifier.emit("feeds_restored")
        self.assertEqual(self.warnings.warnings(), [])

    def test_disabled_warning_is_raised_and_cleared(self):
        self.notifier.emit("plugin_disabled")
        self.assertIn("disabled", self.warnings.warnings()[0])
        self.notifier.emit("plugin_enabled")
        self.assertEqual(self.warnings.warnings(), [])

    def test_center_warning_is_raised_and_cleared(self):
        self.notifier.emit("center_unset")
        self.assertEqual(len(self.warnings.warnings()), 1)
        self.notifier.emit("center_set")
        self.assertEqual(self.warnings.warnings(), [])

    def test_only_the_named_fixture_clears(self):
        self.notifier.emit("fixture_invalid", {"fixture": "A"})
        self.notifier.emit("fixture_invalid", {"fixture": "B"})
        self.notifier.emit("fixture_resolved", {"fixture": "A"})
        remaining = self.warnings.warnings()
        self.assertEqual(len(remaining), 1)
        self.assertIn("'B'", remaining[0])

    def test_blackout_events_raise_no_warning(self):
        self.notifier.emit("blackout_started")
        self.assertEqual(self.warnings.warnings(), [])


class LogSinkTests(unittest.TestCase):
    def setUp(self):
        self.notifier = Notifier([LogSink()])

    def log_of(self, event, details=None, level="INFO"):
        with self.assertLogs("skyguard", level=level) as captured:
            self.notifier.emit(event, details)
        return captured.records[0], "\n".join(captured.output)

    def test_warnings_log_at_warning_level(self):
        record, _ = self.log_of("all_feeds_down")
        self.assertEqual(record.levelno, logging.WARNING)

    def test_transitions_log_reason_and_aircraft(self):
        record, text = self.log_of("blackout_started", {"reason": "aircraft", "aircraft": ["a1"]})
        self.assertEqual(record.levelno, logging.INFO)
        self.assertIn("aircraft", text)
        self.assertIn("a1", text)

    def test_secrets_and_location_are_redacted(self):
        _, text = self.log_of(
            "blackout_started",
            {
                "online_api_key": "hunter2",
                "token": "hunter3",
                "password": "hunter4",
                "center_lat": 12.3456,
                "center_lon": -65.4321,
            },
        )
        for secret in ("hunter2", "hunter3", "hunter4", "12.3456", "65.4321"):
            self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)

    def test_a_newline_in_a_value_cannot_forge_a_log_line(self):
        record, _ = self.log_of("blackout_started", {"callsign": "X\nERROR fake line"})
        self.assertNotIn("\n", record.getMessage())


if __name__ == "__main__":
    unittest.main()
