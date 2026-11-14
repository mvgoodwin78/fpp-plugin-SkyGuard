"""A fake FPP API that behaves like the real source, including its dangerous 9.5-10.2 bug."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

FULL_ON = 255
PHP_ROUTES = ("/api/channel/", "/api/plugin/")


class FakeFpp:
    def __init__(self, delete_bug=True):
        self.delete_bug = delete_bug  # True behaves like FPP 9.5-10.2
        self.ranges = {}
        self.ever_full_on = []
        self.available = True
        self.fail_set = False
        self.fail_delete = False
        self.fail_commands = False
        self.command_error = 503
        self.fail_model_state = False
        self.fail_settings = False
        self.on_setting = None
        self.status = {"status": 0, "status_name": "idle", "scheduler": {"status": "idle"}}
        self.bridge_stats = {"universes": []}
        self.bridge_stats_status = 200
        self.universes = {"channelOutputs": []}
        self.universes_status = 200
        self.fonts = ["Helvetica", "Courier"]
        self.models = {}
        self.model_list = []  # what /api/overlays/models lists
        self.model_list_status = 200
        self.commands = []
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, doc=None):
                body = json.dumps({"Status": "OK"} if doc is None else doc).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length)) if length else None

            def _handle(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self._body()
                path = unquote(self.path)
                server.requests.append((self.command, path, body))
                # Real behavior, from a 10.3 box: PHP routes 404 on a JSON header with no body.
                json_header = "application/json" in (self.headers.get("Content-Type") or "")
                if json_header and not length and path.startswith(PHP_ROUTES):
                    self._reply(404, {})
                    return
                if not server.available:
                    self._reply(503, {})
                    return
                code, doc = server.route(self.command, path, body)
                self._reply(code, doc)

            do_GET = do_PUT = do_POST = _handle

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True).start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def restart_fppd(self):
        """Ranges live only in fppd's memory."""
        self.ranges = {}

    def output(self, channel, sequence_value=100):
        """What the channel would actually put out on this frame."""
        value = self.ranges.get(channel, sequence_value)
        return FULL_ON if value == -1 else value

    def route(self, method, path, body):
        if path == "/api/fppd/status":
            return 200, self.status
        if path == "/api/fppd/e131stats":
            return self.bridge_stats_status, self.bridge_stats
        if path == "/api/channel/output/universeOutputs":
            return self.universes_status, self.universes
        if path == "/api/overlays/models":
            return self.model_list_status, self.model_list
        if path == "/api/overlays/fonts":
            return 200, self.fonts
        if path.startswith("/api/plugin/fpp-plugin-SkyGuard/settings/"):
            if self.fail_settings:
                return 500, {}
            if self.on_setting:
                self.on_setting(path.rsplit("/", 1)[1], body)
            return 200, None
        if path.startswith("/api/overlays/range/"):
            return self._range(path.rsplit("/", 1)[1], body)
        if path.startswith("/api/overlays/model/"):
            return self._model(method, path.split("/")[4:], body)
        if path.startswith("/api/command/"):
            return self._command(path.split("/", 3)[3], body)
        return 404, {}

    def _range(self, spec, body):
        delete = bool(body.get("delete"))
        if delete and self.fail_delete or not delete and self.fail_set:
            return 500, {}
        for part in spec.split(","):
            start, _, end = part.partition("-")
            channel = int(start)
            assert int(end) == channel, "the plugin only uses one-channel ranges"
            if not delete:
                self.ranges[channel] = int(body["Value"])
            elif channel in self.ranges:
                del self.ranges[channel]
            elif self.delete_bug:
                self.ranges[channel] = -1
                self.ever_full_on.append(channel)
        return 200, None

    def _model(self, method, parts, body):
        model = self.models.get(parts[0])
        if model is None:
            return 404, {}
        if method == "GET":
            return 200, {"isActive": model["state"], "effectRunning": model["effect"] is not None}
        if parts[1:] == ["state"]:
            if self.fail_model_state:
                return 500, {}
            model["state"] = int(body["State"])
            return 200, None
        return 404, {}

    def _command(self, name, args):
        self.commands.append((name, args))
        if self.fail_commands:
            return self.command_error, {}
        if name != "Overlay Model Effect":
            return 404, {}
        model = self.models.get(args[0])
        if model is None:
            return 500, {}
        if args[2] == "Text":
            if args[1] != "False":
                model["auto_enable_used"] = True
            model["effect"] = args[-1]
        elif args[2] == "Stop Effects":
            model["effect"] = None
            if args[1] != "False":
                model["state"] = 0
        return 200, None

    def add_model(self, name, state=0):
        self.models[name] = {"state": state, "effect": None}
