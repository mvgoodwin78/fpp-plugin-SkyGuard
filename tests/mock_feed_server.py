"""A mock feed server that replays aircraft.json files, for repeatable tests without real traffic.

Tests use MockFeedServer directly. For acceptance tests on an FPP box, run it by hand:

    python3 tests/mock_feed_server.py --port 8090 sample1.json sample2.json

and set the receiver URL to http://127.0.0.1:8090/data/aircraft.json. It serves each file in
turn, repeating the last, and rewrites `now` so the feed looks live. It listens on loopback only.
"""

import argparse
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOOPBACK = "127.0.0.1"


class MockRoute:
    """How one path behaves. Tests change these fields while the server runs."""

    def __init__(self, docs, now_scale=1.0):
        self.docs = list(docs)
        self.now_scale = now_scale  # 1000 for feeds that report milliseconds
        self.status = 200
        self.delay_s = 0.0
        self.frozen = False
        self.raw_body = None
        self.requests = 0
        self._now = 1793900000.0

    def respond(self):
        """Return (status, body bytes) for one request."""
        self.requests += 1
        if self.status != 200:
            return self.status, b"{}"
        if self.raw_body is not None:
            return 200, self.raw_body
        index = min(self.requests - 1, len(self.docs) - 1)
        doc = copy.deepcopy(self.docs[index])
        if not self.frozen:
            self._now += 1.0
        doc["now"] = self._now * self.now_scale
        return 200, json.dumps(doc).encode("utf-8")


class MockFeedServer:
    def __init__(self, port=0):
        self.routes = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                route = server.routes.get(self.path.split("?")[0])
                if route is None:
                    for prefix, candidate in server.routes.items():
                        if prefix.endswith("*") and self.path.startswith(prefix[:-1]):
                            route = candidate
                            break
                if route is None:
                    self.send_error(404)
                    return
                status, body = route.respond()
                if route.delay_s:
                    time.sleep(route.delay_s)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up, which is what timeout tests do

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer((LOOPBACK, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = None

    def url(self, path):
        return f"http://{LOOPBACK}:{self.port}{path}"

    def add(self, path, docs, now_scale=1.0):
        route = MockRoute(docs, now_scale)
        self.routes[path] = route
        return route

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="Replay aircraft.json files on loopback.")
    parser.add_argument("files", nargs="+", help="recorded aircraft.json files, served in turn")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--path", default="/data/aircraft.json")
    args = parser.parse_args()

    docs = []
    for name in args.files:
        with open(name, encoding="utf-8") as handle:
            docs.append(json.load(handle))
    server = MockFeedServer(args.port)
    server.add(args.path, docs)
    print(f"Serving {len(docs)} file(s) at {server.url(args.path)}")
    server._httpd.serve_forever()


if __name__ == "__main__":
    main()
