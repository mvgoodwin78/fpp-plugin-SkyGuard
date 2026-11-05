"""The real HTTP path and threads, against the mock feed server."""

import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from skyguard.config import Settings
from skyguard.feed_manager import FEED_ONLINE, FEED_RECEIVER, FeedManager
from skyguard.feeds.base import FeedError
from skyguard.feeds.http_client import MAX_RESPONSE_BYTES, FeedHttpError, fetch_json
from skyguard.feeds.online_adsbx_v2 import PROVIDERS, Provider
from skyguard.feeds.receiver import parse_receiver
from skyguard.notifier import Notifier, WarningSink
from tests.mock_feed_server import MockFeedServer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
RECEIVER_PATH = "/data/aircraft.json"
SECOND_PATH = "/978/aircraft.json"


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def wait_until(condition, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.server = MockFeedServer().start()
        self.addCleanup(self.server.stop)


class FetchJsonTests(ServerTestCase):
    def test_returns_parsed_json(self):
        self.server.add("/x", [{"aircraft": []}])
        doc = fetch_json(self.server.url("/x"))
        self.assertEqual(doc["aircraft"], [])

    def test_recorded_files_parse_through_the_whole_path(self):
        for name in ("receiver_readsb.json", "receiver_dump1090fa.json", "receiver_dump978fa.json"):
            with self.subTest(name=name):
                self.server.add("/x", [load(name)])
                snapshot = parse_receiver(fetch_json(self.server.url("/x")))
                self.assertTrue(snapshot.aircraft)

    def test_http_errors_carry_the_status(self):
        route = self.server.add("/x", [{}])
        for status in (404, 429, 500, 503):
            route.status = status
            with self.subTest(status=status):
                with self.assertRaises(FeedHttpError) as caught:
                    fetch_json(self.server.url("/x"))
                self.assertEqual(caught.exception.status, status)

    def test_slow_server_times_out(self):
        route = self.server.add("/x", [{}])
        route.delay_s = 1.0
        started = time.monotonic()
        with self.assertRaises(FeedError) as caught:
            fetch_json(self.server.url("/x"), timeout_s=0.1)
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertIn("timed out", str(caught.exception))

    def test_a_server_dripping_bytes_cannot_outlast_the_time_limit(self):
        class Drip(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        started = time.monotonic()
        with self.assertRaises(FeedError) as caught:
            fetch_json(f"http://127.0.0.1:{httpd.server_address[1]}/", timeout_s=0.2)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("timed out", str(caught.exception))

    def test_invalid_json_is_a_failed_poll(self):
        self.server.add("/x", [{}]).raw_body = b"<html>not json</html>"
        with self.assertRaises(FeedError):
            fetch_json(self.server.url("/x"))

    def test_invalid_utf8_is_a_failed_poll(self):
        self.server.add("/x", [{}]).raw_body = b"\xff\xfe{}"
        with self.assertRaises(FeedError):
            fetch_json(self.server.url("/x"))

    def test_oversized_response_is_refused(self):
        self.server.add("/x", [{}]).raw_body = b" " * (MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(FeedError) as caught:
            fetch_json(self.server.url("/x"))
        self.assertIn("too large", str(caught.exception))

    def test_connection_refused_is_a_failed_poll(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(FeedError):
            fetch_json(f"http://127.0.0.1:{port}/", timeout_s=0.5)

    def test_non_http_urls_are_refused_without_opening_them(self):
        for url in ("file:///etc/passwd", "ftp://example.invalid/x"):
            with self.subTest(url=url):
                with self.assertRaises(FeedError) as caught:
                    fetch_json(url)
                self.assertEqual(str(caught.exception), "unsupported address")

    def test_errors_do_not_contain_the_url(self):
        self.server.add("/secret-path", [{}]).status = 500
        with self.assertRaises(FeedError) as caught:
            fetch_json(self.server.url("/secret-path"))
        self.assertNotIn("secret-path", str(caught.exception))

    def test_identifies_itself_and_not_as_a_browser_or_library(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["agent"] = self.headers.get("User-Agent")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        fetch_json(f"http://127.0.0.1:{httpd.server_address[1]}/")
        self.assertEqual(seen["agent"], "fpp-plugin-SkyGuard")


class RedirectTests(unittest.TestCase):
    def serve_redirect(self, location):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/go":
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_redirect_to_file_is_refused(self):
        base = self.serve_redirect("file:///etc/passwd")
        with self.assertRaises(FeedError):
            fetch_json(f"{base}/go")

    def test_redirect_to_ftp_is_refused_without_being_followed(self):
        # urllib would follow this one by itself.
        base = self.serve_redirect("ftp://127.0.0.1:1/x")
        with self.assertRaises(FeedError) as caught:
            fetch_json(f"{base}/go")
        self.assertIn("redirected", str(caught.exception))

    def test_redirect_to_http_is_followed(self):
        base = self.serve_redirect("/final")
        self.assertEqual(fetch_json(f"{base}/go"), {"ok": True})


class LiveManagerTests(ServerTestCase):
    """End-to-end with real threads, tiny intervals and the mock server."""

    def setUp(self):
        super().setUp()
        self.receiver = self.server.add(RECEIVER_PATH, [load("receiver_readsb.json")])
        self.online = self.server.add("/online/*", [load("online_adsbx_v2.json")], now_scale=1000.0)
        template = self.server.url("/online/{lat}/{lon}/{nm}")
        patcher = mock.patch.dict(PROVIDERS, {"mock": Provider("mock", template, 0.01)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def manager(self, **overrides):
        values = {
            "source_mode": "both",
            "receiver_url": self.server.url(RECEIVER_PATH),
            "online_provider": "mock",
            "center_lat": 40.0,
            "center_lon": -75.0,
            "poll_interval_s": 0.02,
            "online_idle_interval_s": 0.02,
            "health_interval_s": 0.05,
            "max_data_age_s": 0.3,
        }
        values.update(overrides)
        self.warnings = WarningSink()
        manager = FeedManager(
            Settings(**values),
            Notifier([self.warnings]),
            fetch=lambda url: fetch_json(url, timeout_s=0.2),
        )
        manager.start(tick_s=0.005)
        self.addCleanup(manager.stop)
        return manager

    def test_receiver_data_flows(self):
        manager = self.manager()
        self.assertTrue(wait_until(lambda: manager.state().aircraft))
        self.assertTrue(manager.data_fresh())
        self.assertEqual(manager.state().active_feed, FEED_RECEIVER)
        self.assertEqual(self.online.requests, 0)

    def test_receiver_dies_online_takes_over_receiver_returns(self):
        manager = self.manager()
        self.assertTrue(wait_until(lambda: manager.state().aircraft))

        self.receiver.status = 503
        self.assertTrue(wait_until(lambda: manager.state().active_feed == FEED_ONLINE))
        self.assertTrue(wait_until(lambda: manager.state().aircraft[0].source == "online:mock"))
        self.assertTrue(wait_until(manager.data_fresh))
        self.assertEqual(len(self.warnings.warnings()), 2)

        self.receiver.status = 200
        self.assertTrue(wait_until(lambda: manager.state().active_feed == FEED_RECEIVER))
        self.assertTrue(wait_until(lambda: manager.state().aircraft[0].source == "receiver"))
        self.assertEqual(self.warnings.warnings(), [])

    def test_both_feeds_down_then_one_returns(self):
        manager = self.manager()
        self.assertTrue(wait_until(lambda: manager.state().aircraft))

        self.receiver.status = 503
        self.online.status = 503
        self.assertTrue(wait_until(lambda: not manager.data_fresh()))
        self.assertTrue(
            wait_until(lambda: any("No aircraft data" in w for w in self.warnings.warnings()))
        )

        self.online.status = 200
        self.assertTrue(wait_until(manager.data_fresh))
        self.assertTrue(
            wait_until(lambda: not any("No aircraft data" in w for w in self.warnings.warnings()))
        )

    def test_a_frozen_receiver_goes_stale_and_falls_back(self):
        manager = self.manager()
        self.assertTrue(wait_until(lambda: manager.state().aircraft))
        self.receiver.frozen = True
        self.assertTrue(wait_until(lambda: manager.state().active_feed == FEED_ONLINE))

    def test_a_hung_second_decoder_does_not_delay_the_primary(self):
        second = self.server.add(SECOND_PATH, [load("receiver_dump978fa.json")])
        second.delay_s = 0.5
        manager = self.manager(
            source_mode="receiver",
            receiver_978_url=self.server.url(SECOND_PATH),
        )
        self.assertTrue(wait_until(lambda: manager.state().aircraft))
        before = self.receiver.requests
        time.sleep(0.3)
        self.assertGreater(self.receiver.requests - before, 3)
        self.assertTrue(manager.data_fresh())

    def test_a_hung_health_check_does_not_delay_online_polling(self):
        manager = self.manager()
        self.assertTrue(wait_until(lambda: manager.state().aircraft))
        self.receiver.status = 503
        self.assertTrue(wait_until(lambda: manager.state().active_feed == FEED_ONLINE))
        self.receiver.delay_s = 0.5  # still failing, and now slow to do so
        before = self.online.requests
        time.sleep(0.3)
        self.assertGreater(self.online.requests - before, 3)

    def test_an_unexpected_error_does_not_end_the_loop(self):
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("surprise")
            return fetch_json(url, timeout_s=0.2)

        manager = FeedManager(
            Settings(
                source_mode="receiver",
                receiver_url=self.server.url(RECEIVER_PATH),
                poll_interval_s=0.02,
                max_data_age_s=0.3,
            ),
            Notifier(),
            fetch=flaky,
        )
        with self.assertLogs("skyguard", level="ERROR"):
            manager.start(tick_s=0.005)
            self.addCleanup(manager.stop)
            self.assertTrue(wait_until(lambda: manager.state().aircraft))

    def test_stop_ends_polling_quickly(self):
        manager = self.manager()
        self.assertTrue(wait_until(lambda: manager.state().aircraft))
        started = time.monotonic()
        manager.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        before = self.receiver.requests
        time.sleep(0.1)
        self.assertLessEqual(self.receiver.requests - before, 1)


class MockServerTests(ServerTestCase):
    def test_replays_files_in_turn_and_repeats_the_last(self):
        self.server.add("/x", [{"aircraft": [1]}, {"aircraft": [2]}])
        url = self.server.url("/x")
        self.assertEqual(fetch_json(url)["aircraft"], [1])
        self.assertEqual(fetch_json(url)["aircraft"], [2])
        self.assertEqual(fetch_json(url)["aircraft"], [2])

    def test_now_advances_unless_frozen(self):
        route = self.server.add("/x", [{"aircraft": []}])
        url = self.server.url("/x")
        first = fetch_json(url)["now"]
        self.assertGreater(fetch_json(url)["now"], first)
        route.frozen = True
        held = fetch_json(url)["now"]
        self.assertEqual(fetch_json(url)["now"], held)

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.server._httpd.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
