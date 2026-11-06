"""The plugin as FPP sees it: metadata, scripts and the full lifecycle on Linux."""

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from skyguard.config import Paths, Settings
from skyguard.matrix import SAMPLE_VALUES
from skyguard.feeds.online_adsbx_v2 import PROVIDERS
from skyguard.fpp_api import FppApi
from skyguard.overlay import Ledger, OverlayController
from skyguard.notifier import Notifier
from skyguard.release import release_everything
from tests.fake_fpp_server import FakeFpp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "fpp-plugin-SkyGuard"
HOOKS = ("fpp_install.sh", "fpp_uninstall.sh", "postStart.sh", "preStop.sh")
SHELL_SCRIPTS = ("callbacks", *(f"scripts/{n}" for n in (*HOOKS, "skyguard_start.sh")))
COMMAND_SCRIPTS = ("skyguard_enable.py", "skyguard_disable.py", "skyguard_test.py", "skyguard_test_message.py",
                   "skyguard_stop_test_message.py")
EXECUTABLE = (*SHELL_SCRIPTS, *(f"commands/{n}" for n in COMMAND_SCRIPTS))


def read(relpath):
    with open(os.path.join(ROOT, relpath), encoding="utf-8", newline="") as handle:
        return handle.read()


def load(relpath):
    return json.loads(read(relpath))


class PluginInfoTests(unittest.TestCase):
    def setUp(self):
        self.info = load("pluginInfo.json")
        self.privacy = self.info["privacy"]

    def test_required_fields(self):
        for field in ("repoName", "name", "author", "description", "homeURL", "srcURL", "bugURL", "versions"):
            self.assertTrue(self.info.get(field), field)
        self.assertEqual(self.info["repoName"], REPO)
        self.assertTrue(self.info["srcURL"].endswith(f"{REPO}.git"))

    def test_nothing_left_from_the_template(self):
        text = read("pluginInfo.json")
        for leftover in ("TEMPLATE", "Template", "John Doe", "CoolPlugin", "FalconChristmas"):
            self.assertNotIn(leftover, text)

    def test_versions_start_at_9_5_on_the_exact_platform_names(self):
        versions = self.info["versions"]
        self.assertEqual(versions[0]["minFPPVersion"], "9.5")
        for version in versions:
            self.assertEqual(version["platforms"], ["Raspberry Pi", "BeagleBone Black", "BeagleBone 64"])
            self.assertEqual(version["branch"], "master")

    def test_privacy_has_exactly_the_eight_keys(self):
        self.assertEqual(
            set(self.privacy),
            {"summary", "sends", "collects", "sensors", "remoteAccess", "systemChanges", "closedCode", "other"},
        )

    def test_privacy_text_is_within_the_limits(self):
        self.assertLessEqual(len(self.privacy["summary"]), 200)
        for send in self.privacy["sends"]:
            self.assertLessEqual(len(send["what"]), 100)
            self.assertLessEqual(len(send["why"]), 100)
            self.assertEqual(send["why"], send["why"].rstrip("."), "FPP adds its own full stop")
            self.assertEqual(send["why"][0], send["why"][0].lower())
        for item in self.privacy["collects"]:
            self.assertLessEqual(len(item["what"]), 100)

    def test_every_host_in_the_code_is_disclosed_in_both_places(self):
        hosts = {re.match(r"https://([^/]+)/", p.url_template).group(1) for p in PROVIDERS.values()}
        hosts.add("nominatim.openstreetmap.org")
        declared = {send["to"] for send in self.privacy["sends"]}
        for host in hosts:
            self.assertIn(host, declared)
            self.assertIn(host, self.info["description"])

    def test_nothing_is_marked_as_sending_on_its_own(self):
        self.assertEqual([send["to"] for send in self.privacy["sends"] if send["alwaysOn"]], ["tile.openstreetmap.org"])

    def test_the_location_is_disclosed_as_kept(self):
        self.assertIn("private.json", self.privacy["collects"][0]["where"])

    def test_routes_that_act_are_named_in_other(self):
        for word in ("Enable", "Disable", "Test Trigger", "location"):
            self.assertIn(word, self.privacy["other"])

    def test_no_api_key_is_ever_placed_in_the_config_folder(self):
        self.assertNotIn("config/", self.privacy["collects"][0]["where"])


class CommandTests(unittest.TestCase):
    def test_descriptions_name_scripts_that_exist(self):
        commands = load("commands/descriptions.json")
        self.assertEqual(
            [c["name"] for c in commands],
            ["SkyGuard Enable", "SkyGuard Disable", "SkyGuard Test Trigger", "SkyGuard Test Message",
             "SkyGuard Stop Test Message"],
        )
        for command in commands:
            self.assertTrue(os.path.exists(os.path.join(ROOT, "commands", command["script"])))

    def test_the_message_test_script_sends_its_command(self):
        self.assertEqual(self.run_script("skyguard_test_message.py")["command"], "test_message")
        self.assertEqual(self.run_script("skyguard_stop_test_message.py")["command"], "stop_test_message")

    def test_only_the_test_trigger_takes_an_argument(self):
        commands = {c["name"]: c for c in load("commands/descriptions.json")}
        self.assertEqual(commands["SkyGuard Enable"]["args"], [])
        self.assertEqual(commands["SkyGuard Disable"]["args"], [])
        self.assertEqual(commands["SkyGuard Test Message"]["args"], [])
        self.assertEqual(commands["SkyGuard Stop Test Message"]["args"], [])
        argument = commands["SkyGuard Test Trigger"]["args"][0]
        self.assertEqual((argument["min"], argument["max"], argument["default"]), (1, 3600, "10"))
        fixture = commands["SkyGuard Test Trigger"]["args"][1]
        self.assertEqual((fixture["name"], fixture["optional"]), ("fixture", True))

    def test_scripts_use_an_absolute_python_path(self):
        # FPP runs them with no PATH in the environment.
        for name in COMMAND_SCRIPTS:
            self.assertTrue(read(f"commands/{name}").startswith("#!/usr/bin/python3\n"), name)

    def run_script(self, name, *args):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        # Exactly the environment FPP gives a command script, plus what Windows needs to start.
        env = {"MEDIADIR": media, "FPPDIR": "/opt/fpp", "SCRIPTDIR": os.path.join(ROOT, "commands")}
        for key in ("SYSTEMROOT", "PATH"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic()
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "commands", name), *args],
            env=env, cwd=tempfile.gettempdir(), check=True, timeout=20, capture_output=True,
        )
        self.assertLess(time.monotonic() - started, 10)
        spool = Paths.from_env({"MEDIADIR": media}).commands_dir
        files = os.listdir(spool)
        self.assertEqual(len(files), 1)
        with open(os.path.join(spool, files[0]), encoding="utf-8") as handle:
            return json.load(handle)

    def test_enable_script(self):
        self.assertEqual(self.run_script("skyguard_enable.py")["command"], "enable")

    def test_disable_script(self):
        self.assertEqual(self.run_script("skyguard_disable.py")["command"], "disable")

    def test_test_script_passes_the_seconds(self):
        doc = self.run_script("skyguard_test.py", "25")
        self.assertEqual((doc["command"], doc["seconds"]), ("test", "25"))

    def test_test_script_passes_the_fixture(self):
        doc = self.run_script("skyguard_test.py", "3", "abc")
        self.assertEqual((doc["seconds"], doc["fixture"]), ("3", "abc"))

    def test_test_script_treats_an_empty_fixture_as_all_heads(self):
        self.assertNotIn("fixture", self.run_script("skyguard_test.py", "3", ""))

    def test_test_script_without_seconds_uses_the_default_later(self):
        self.assertNotIn("seconds", self.run_script("skyguard_test.py"))


class ScriptFileTests(unittest.TestCase):
    def test_scripts_have_unix_line_endings(self):
        for name in (*SHELL_SCRIPTS, *(f"commands/{n}" for n in COMMAND_SCRIPTS)):
            self.assertNotIn("\r", read(name), name)

    def test_scripts_have_a_shebang(self):
        for name in SHELL_SCRIPTS:
            self.assertTrue(read(name).startswith("#!/bin/bash\n"), name)

    def test_no_forbidden_commands(self):
        for name in SHELL_SCRIPTS:
            text = "\n".join(l for l in read(name).splitlines() if not l.lstrip().startswith("#"))
            for word in (r"\bsudo\b", r"\breboot\b", r"curl .*\|", r"32322", r"systemctl", r"fppd restart"):
                self.assertIsNone(re.search(word, text), f"{name}: {word}")

    def test_install_stops_on_failure_from_the_first_command(self):
        lines = [l for l in read("scripts/fpp_install.sh").splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(lines[0], "set -e")

    def test_uninstall_stops_the_daemon_before_releasing(self):
        # Otherwise the daemon could set a range again after the release.
        script = read("scripts/fpp_uninstall.sh")
        self.assertLess(script.index("skyguard.supervisor stop"), script.index("skyguard.release"))

    def test_the_daemon_is_started_in_the_background_with_nothing_attached(self):
        # fppd waits on these scripts, and on anything that holds their output open.
        self.assertIn("</dev/null >/dev/null 2>&1 &", read("scripts/skyguard_start.sh"))

    def test_the_template_scripts_are_gone(self):
        for name in ("commands/on.sh", "scripts/preStart.sh", "scripts/fpp_update_check.sh", "output.php"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)), name)

    def test_exec_bits_are_set_in_git(self):
        # On Windows the file system can't show this, and FPP silently skips a hook without it.
        try:
            output = subprocess.run(
                ["git", "ls-files", "-s", *EXECUTABLE],
                cwd=ROOT, check=True, capture_output=True, text=True, timeout=20,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        if not output:
            self.skipTest("not a git checkout")
        modes = {line.split("\t")[1]: line.split()[0] for line in output.splitlines()}
        for name in EXECUTABLE:
            self.assertEqual(modes.get(name), "100755", name)


class PhpTests(unittest.TestCase):
    """Text checks only: no PHP is available here, so these don't prove it parses."""

    def setUp(self):
        self.api = read("api.php")

    def test_the_registrar_uses_the_name_fpp_looks_for(self):
        self.assertIn("function getEndpointsfpppluginSkyGuard()", self.api)

    def test_every_function_name_is_prefixed(self):
        for name in re.findall(r"^function (\w+)", self.api, re.MULTILINE):
            self.assertTrue(name.startswith(("fppPluginSkyGuard", "getEndpointsfpppluginSkyGuard")), name)

    def test_every_callback_exists(self):
        for name in re.findall(r"'callback' => '(\w+)'", self.api):
            self.assertIn(f"function {name}(", self.api)

    def test_nothing_runs_when_the_file_loads(self):
        depth = 0
        outside = []
        for line in self.api.splitlines()[1:]:
            stripped = line.strip()
            if depth == 0 and stripped and not stripped.startswith(("//", "function ")):
                outside.append(stripped)
            depth += line.count("{") - line.count("}")
        self.assertEqual(outside, [])

    def test_balanced_braces_and_parentheses(self):
        code = re.sub(r"//[^\n]*|'(?:[^'\\]|\\.)*'", "''", self.api)
        for opening, closing in ("{}", "()", "[]"):
            self.assertEqual(code.count(opening), code.count(closing), opening)

    def test_every_route_is_documented(self):
        paths = load("apiDocs.json")["paths"]
        routes = re.findall(r"'method' => '(\w+)', 'endpoint' => '([^']+)'", self.api)
        self.assertGreater(len(routes), 10)
        for method, endpoint in routes:
            path = re.sub(r":(\w+)", r"{\1}", endpoint)
            self.assertIn(method.lower(), paths[f"/api/plugin/{REPO}/{path}"], endpoint)

    def test_the_tooltip_is_escaped_for_the_attribute_fpp_puts_it_in(self):
        self.assertIn("htmlspecialchars($tooltip, ENT_QUOTES)", self.api)

    def test_the_lookup_has_time_limits_and_identifies_itself(self):
        for option in ("CURLOPT_CONNECTTIMEOUT", "CURLOPT_TIMEOUT", "CURLOPT_USERAGENT"):
            self.assertIn(option, self.api)

    def test_the_address_is_never_logged(self):
        self.assertNotIn("error_log", self.api)
        self.assertNotIn("file_put_contents($logFile", self.api)

    def test_the_api_key_is_never_returned_or_logged(self):
        self.assertNotIn("error_log", self.api)
        body = self.api.split("function fppPluginSkyGuardPrivateGet(")[1].split("\n}\n")[0]
        self.assertIn("'api_key_set' => !empty($doc['online_api_key'])", body)
        self.assertNotIn("online_api_key", body.replace("!empty($doc['online_api_key'])", ""))
        self.assertNotIn("json($doc)", body)

    def test_the_private_file_is_kept_private(self):
        self.assertIn("0600", self.api)

    def test_every_write_needs_a_json_request(self):
        # A page on another site can't send this without a preflight FPP doesn't allow.
        self.assertIn("application/json", self.api)
        for name in ("PrivatePut", "FixtureAdd", "FixtureUpdate", "TestReceiver", "TestOnline"):
            body = self.api.split(f"function fppPluginSkyGuard{name}(")[1].split("\nfunction ")[0]
            self.assertIn("fppPluginSkyGuardJsonBody()", body, name)

    def test_the_receiver_test_only_allows_http_and_follows_no_redirects(self):
        body = self.api.split("function fppPluginSkyGuardTestReceiver(")[1].split("\nfunction ")[0]
        self.assertIn("'http'", body)
        fetch = self.api.split("function fppPluginSkyGuardFetchForTest(")[1].split("\nfunction ")[0]
        self.assertIn("CURLOPT_FOLLOWLOCATION, false", fetch)
        self.assertIn("CURLOPT_TIMEOUT", fetch)

    def test_online_providers_match_the_python_list(self):
        for name, provider in PROVIDERS.items():
            self.assertIn(f"'{name}'", self.api)
            expected = provider.url_template.format(lat="%s", lon="%s", nm=5)
            self.assertIn(f"'{name}' => '{expected}'", self.api)

    def test_writes_are_atomic_and_locked(self):
        self.assertIn("rename(", self.api)
        self.assertIn("flock(", self.api)

    def test_every_loaded_file_exists_under_a_name_fpp_serves(self):
        for page in ("content.php", "status.php"):
            names = re.findall(r"'([\w./-]+\.(?:js|css))'", read(page))
            self.assertGreaterEqual(len(names), 4, page)
            for name in names:
                self.assertRegex(name, r"^[A-Za-z0-9_./-]+$")
                self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", name)), name)

    def test_pages_have_no_hard_coded_colors(self):
        for name in ("content.php", "status.php", "about.php"):
            text = read(name)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b(?!\w)|\brgb\(|style=\"[^\"]*color", text), name)
        script = read("assets/skyguard-settings.js")
        self.assertIsNone(re.search(r"style\.(?:color|background)", script))

    def test_no_external_loads(self):
        for name in ("content.php", "assets/skyguard-settings.js"):
            self.assertIsNone(re.search(r"<script[^>]+src=|<link[^>]+href=", read(name)), name)

    def test_plugin_files_load_with_a_version_because_fpp_lets_browsers_keep_them_a_year(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "js")), "FPP would load js/ with no version")
        self.assertFalse(os.path.exists(os.path.join(ROOT, "css")), "FPP would load css/ with no version")
        self.assertIn("filemtime", read("skyguard_assets.inc"))
        self.assertIn("&v=", read("skyguard_assets.inc"))
        for page in ("content.php", "status.php"):
            self.assertIn("skyguard_assets.inc", read(page))
            self.assertIn("skyGuardScripts(", read(page))
            self.assertIn("skyGuardStyles(", read(page))

    def test_leaflet_is_the_unchanged_release(self):
        expected = {
            "assets/leaflet/leaflet.js": "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
            "assets/leaflet/leaflet.css": "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
        }
        for name, digest in expected.items():
            with open(os.path.join(ROOT, name), "rb") as handle:
                self.assertEqual(base64.b64encode(hashlib.sha256(handle.read()).digest()).decode(), digest, name)
        self.assertIn("BSD 2-Clause", read("assets/leaflet/LICENSE"))

    def test_new_scripts_write_text_not_html_and_new_styles_use_theme_colors(self):
        for name in ("assets/skyguard-status.js", "assets/skyguard-map.js"):
            script = read(name)
            self.assertNotIn("innerHTML", script, name)
            self.assertNotIn(".html(", script, name)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|\brgb\(", script), name)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|\brgb\(", read("assets/skyguard.css")))

    def test_the_map_host_is_allowed_on_install_removed_on_uninstall_and_declared(self):
        host = "https://tile.openstreetmap.org"
        self.assertIn(f'ManageApacheContentPolicy.sh" add img-src {host}', read("scripts/fpp_install.sh"))
        self.assertIn(f'ManageApacheContentPolicy.sh" remove img-src {host}', read("scripts/fpp_uninstall.sh"))
        self.assertIn(host, read("assets/skyguard-map.js"))
        info = json.loads(read("pluginInfo.json"))
        sends = {entry["to"]: entry for entry in info["privacy"]["sends"]}
        self.assertIn("your browser", sends["tile.openstreetmap.org"]["what"])

    def test_script_uses_no_jquery_helpers_removed_in_jquery_4(self):
        script = read("assets/skyguard-settings.js")
        for removed in ("$.trim", "$.isArray", "$.isFunction", ".size()"):
            self.assertNotIn(removed, script)

    def test_a_json_header_is_only_sent_with_a_body(self):
        script = read("assets/skyguard-settings.js")
        self.assertEqual(script.count("application/json"), 1)
        self.assertIn("options.body = JSON.stringify(body)", script)

    def test_script_writes_text_with_textcontent_not_html(self):
        script = read("assets/skyguard-settings.js")
        self.assertNotIn("innerHTML", script)
        self.assertNotIn(".html(", script)

    def test_quotes_are_refused_in_free_text(self):
        # FPP 9.5 writes settings into an inline script without escaping them.
        self.assertIn("hasUnsafeCharacters", read("assets/skyguard-settings.js"))

    def test_every_setting_on_the_page_is_one_the_daemon_knows(self):
        known = set(Settings.__dataclass_fields__) | {"units", "setup_state", "map_tile_url"}
        page = read("content.php") + read("assets/skyguard-settings.js")
        names = set(re.findall(r"skyGuardNumber\('(\w+)'", page))
        names |= set(re.findall(r"skyGuard(?:Select|Checkbox)\('[^']*', '(\w+)'", page))
        names |= set(re.findall(r"data-(?:setting|free-text)=\"(\w+)\"", page))
        names |= set(re.findall(r"saveSetting\('(\w+)'", page))
        self.assertGreater(len(names), 15)
        self.assertEqual(names - known, set())

    def test_the_defaults_shown_are_the_daemons_defaults(self):
        defaults = Settings()
        page = read("content.php")
        shown = re.findall(r"skyGuardNumber\('(\w+)', [-\d.]+, [-\d.]+, '([^']*)'(?:, '[^']*')?\)", page)
        self.assertGreater(len(shown), 8)
        for name, value in shown:
            self.assertEqual(float(value), float(getattr(defaults, name)), name)
        checks = re.findall(r"skyGuardCheckbox\('[^']*', '(\w+)', (\d)\)", page)
        self.assertGreater(len(checks), 2)
        for name, value in checks:
            self.assertEqual(bool(int(value)), getattr(defaults, name), name)
        selects = re.findall(r"skyGuardSelect\('[^']*', '(\w+)', '([^']*)'", page)
        for name, value in selects:
            if name != "units":
                self.assertEqual(value, getattr(defaults, name), name)

    def test_the_radius_fields_have_no_default(self):
        page = read("content.php")
        self.assertIn("skyGuardNumber('radius', 0.01, 99999)", page)
        self.assertIn("skyGuardNumber('radius_receiver', 0.01, 99999)", page)

    def test_rows_without_an_id_get_one(self):
        body = self.api.split("function fppPluginSkyGuardFixtureList(")[1].split("\nfunction ")[0]
        self.assertIn("random_bytes", body)

    def test_the_limits_shown_match_the_daemon(self):
        page = read("content.php")
        for setting, low, high in (("poll_interval_s", 1, 2), ("online_idle_interval_s", 5, 300),
                                   ("ground_speed_limit_kt", 5, 100), ("matrix_size", 4, 100),
                                   ("matrix_speed", 0, 200)):
            self.assertIn(f"skyGuardNumber('{setting}', {low}, {high}", page)

    def test_the_preview_and_the_panel_test_use_the_same_samples(self):
        script = read("assets/skyguard-settings.js")
        block = script.split("var PLACEHOLDERS = {")[1].split("}")[0]
        shown = dict(re.findall(r"(\w+): '([^']*)'", block))
        self.assertEqual(shown, SAMPLE_VALUES)

    def test_the_header_color_is_one_fpp_accepts(self):
        colors = re.findall(r"\$color = '([^']*)'", self.api)
        self.assertEqual(len(colors), 2)
        for color in colors:
            self.assertRegex(color, r"^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]+$")

    def test_the_personal_use_step_has_no_skip(self):
        script = read("assets/skyguard-settings.js")
        self.assertIn("personal_use_confirmed", script)
        self.assertIn("step.skippable", script)

    def test_menu_has_at_most_one_entry_per_type(self):
        types = re.findall(r"'type' => '(\w+)'", read("menu.inc"))
        self.assertEqual(sorted(types), ["content", "help", "status"])


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.paths = Paths.from_env({"MEDIADIR": self.media})
        os.makedirs(self.paths.data_dir)
        self.fpp = FakeFpp()
        self.addCleanup(self.fpp.stop)
        self.api = FppApi(self.fpp.url, timeout_s=1.0)

    def hold(self):
        ledger = Ledger(self.paths.ledger_file)
        OverlayController(self.api, ledger, Notifier(), time.monotonic).sync({10: 0, 11: 8})

    def test_releases_what_the_ledger_holds(self):
        self.hold()
        self.assertTrue(release_everything(self.paths, self.api))
        self.assertEqual(self.fpp.ranges, {})
        self.assertEqual(self.fpp.ever_full_on, [])

    def test_never_goes_full_on_if_fppd_restarted(self):
        self.hold()
        self.fpp.restart_fppd()
        self.assertTrue(release_everything(self.paths, self.api))
        self.assertEqual(self.fpp.ever_full_on, [])

    def test_running_twice_or_with_nothing_held_is_safe(self):
        self.assertTrue(release_everything(self.paths, self.api))
        self.hold()
        self.assertTrue(release_everything(self.paths, self.api))
        self.assertTrue(release_everything(self.paths, self.api))
        self.assertEqual(self.fpp.ever_full_on, [])

    def test_reports_failure_if_fpp_is_away(self):
        self.hold()
        self.fpp.available = False
        with self.assertLogs("skyguard", level="WARNING"):
            self.assertFalse(release_everything(self.paths, self.api))

    def test_clears_a_message_left_on_a_panel(self):
        self.fpp.add_model("Panel", state=1)
        self.fpp.models["Panel"]["effect"] = "text"
        ledger = Ledger(self.paths.ledger_file)
        ledger.matrix["Panel"] = 0
        ledger.save()
        self.assertTrue(release_everything(self.paths, self.api))
        self.assertIsNone(self.fpp.models["Panel"]["effect"])
        self.assertEqual(self.fpp.models["Panel"]["state"], 0)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs Linux to run the real scripts")
class LifecycleTests(unittest.TestCase):
    """The real scripts, in a copied plugin folder, with a fake FPP install around them."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.fppdir = os.path.join(self.base, "fpp")
        self.media = os.path.join(self.base, "media")
        self.plugin = os.path.join(self.media, "plugins", REPO)
        os.makedirs(os.path.join(self.fppdir, "scripts"))
        os.makedirs(os.path.join(self.fppdir, "src"))
        os.makedirs(os.path.join(self.media, "config"))
        os.makedirs(os.path.join(self.media, "logs"))
        os.makedirs(self.plugin)
        for name in ("skyguard", "commands", "scripts"):
            shutil.copytree(os.path.join(ROOT, name), os.path.join(self.plugin, name))
        shutil.copy(os.path.join(ROOT, "callbacks"), self.plugin)
        for name in EXECUTABLE:
            path = os.path.join(self.plugin, name)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.flag_file = os.path.join(self.base, "restartFlag")
        with open(os.path.join(self.fppdir, "scripts", "common"), "w", encoding="utf-8") as handle:
            handle.write(
                f'MEDIADIR="{self.media}"\nLOGDIR="{self.media}/logs"\nFPPBINDIR="{self.fppdir}/src"\n'
                f'setSetting() {{ echo "$1=$2" >> "{self.flag_file}"; }}\n'
            )
        self.set_fpp_version("10.3")
        self.paths = Paths.from_env({"MEDIADIR": self.media})
        self.addCleanup(self.run_hook, "scripts/preStop.sh")

    def set_fpp_version(self, version, answer=None):
        """A stand-in for curl, giving the answer a real FPP 10.3 gave to /api/fppd/version."""
        major, minor = version.split(".")
        answer = answer or json.dumps({
            "Message": "", "Status": "OK", "branch": f"v{version}", "fppdAPI": "v1",
            "majorVersion": major, "minorVersion": minor, "respCode": 200, "version": version,
        })
        bin_dir = os.path.join(self.base, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        path = os.path.join(bin_dir, "curl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/bash\necho '{answer}'\n")
        os.chmod(path, 0o755)

    def run_hook(self, name, *args, **env):
        return subprocess.run(
            [os.path.join(self.plugin, name), *args],
            env={
                **os.environ,
                "FPPDIR": self.fppdir,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": os.path.join(self.base, "bin") + os.pathsep + os.environ["PATH"],
                **env,
            },
            capture_output=True, text=True, timeout=20,
        )

    def wait_for(self, condition, timeout_s=10.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return condition()

    def supervisor_running(self):
        return os.path.exists(self.paths.supervisor_pid_file)

    def supervisors(self):
        marker = "skyguard.supervisor"
        count = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    cmdline = handle.read()
                with open(f"/proc/{entry}/environ", "rb") as handle:
                    environ = handle.read()
            except OSError:
                continue
            if marker.encode() in cmdline and self.media.encode() in environ:
                count += 1
        return count

    def test_install_creates_a_private_folder_and_is_safe_twice(self):
        for _ in range(2):
            result = self.run_hook("scripts/fpp_install.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isdir(self.paths.commands_dir))
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.private_file).st_mode), 0o600)

    def test_install_tightens_a_private_file_that_is_too_open(self):
        self.run_hook("scripts/fpp_install.sh")
        os.chmod(self.paths.private_file, 0o644)
        self.run_hook("scripts/fpp_install.sh")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.private_file).st_mode), 0o600)

    def test_install_keeps_an_existing_setup(self):
        self.run_hook("scripts/fpp_install.sh")
        with open(self.paths.private_file, "w", encoding="utf-8") as handle:
            json.dump({"center_lat": 1, "center_lon": 2}, handle)
        self.run_hook("scripts/fpp_install.sh")
        with open(self.paths.private_file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["center_lat"], 1)

    def test_no_restart_is_asked_for_on_fpp_10(self):
        self.run_hook("scripts/fpp_install.sh")
        self.assertFalse(os.path.exists(self.flag_file))

    def test_a_restart_is_asked_for_on_fpp_9(self):
        self.set_fpp_version("9.5")
        self.run_hook("scripts/fpp_install.sh")
        with open(self.flag_file, encoding="utf-8") as handle:
            self.assertIn("restartFlag=1", handle.read())

    def test_a_restart_is_asked_for_if_the_version_cannot_be_read(self):
        self.set_fpp_version("10.3", answer="not json")
        self.assertEqual(self.run_hook("scripts/fpp_install.sh").returncode, 0)
        self.assertTrue(os.path.exists(self.flag_file))

    def test_a_number_for_the_major_version_is_read_too(self):
        self.set_fpp_version("10.3", answer='{"version": "10.3", "majorVersion": 10, "minorVersion": 3}')
        self.run_hook("scripts/fpp_install.sh")
        self.assertFalse(os.path.exists(self.flag_file))

    def test_the_version_comes_from_the_api_because_the_fpp_tool_has_no_version_option(self):
        for name in ("scripts/fpp_install.sh", "scripts/fpp_uninstall.sh"):
            self.assertIn("/api/fppd/version", read(name))
            self.assertNotIn(" -V", read(name))

    def test_post_start_returns_at_once_and_the_daemon_runs(self):
        self.run_hook("scripts/fpp_install.sh")
        started = time.monotonic()
        self.assertEqual(self.run_hook("scripts/postStart.sh").returncode, 0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(self.wait_for(self.supervisor_running))
        self.assertTrue(self.wait_for(lambda: os.path.exists(self.paths.status_file)))

    def test_an_update_restarts_a_running_daemon_so_it_runs_the_new_code(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        with open(self.paths.supervisor_pid_file, encoding="utf-8") as handle:
            before = handle.read().strip()
        self.assertEqual(self.run_hook("scripts/fpp_install.sh").returncode, 0)

        def restarted():
            try:
                with open(self.paths.supervisor_pid_file, encoding="utf-8") as handle:
                    return handle.read().strip() not in ("", before)
            except OSError:
                return False

        self.assertTrue(self.wait_for(restarted))
        time.sleep(0.5)
        self.assertEqual(self.supervisors(), 1)

    def test_a_new_install_does_not_start_the_daemon_itself(self):
        self.assertEqual(self.run_hook("scripts/fpp_install.sh").returncode, 0)
        time.sleep(0.5)
        self.assertEqual(self.supervisors(), 0)

    def test_starting_twice_leaves_one_supervisor(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/postStart.sh")
        self.run_hook("callbacks", "--type", "lifecycle", "startup")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        time.sleep(1.0)
        self.assertEqual(self.supervisors(), 1)

    def test_pre_stop_ends_it_within_the_budget(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        started = time.monotonic()
        self.assertEqual(self.run_hook("scripts/preStop.sh").returncode, 0)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual(self.supervisors(), 0)

    def test_pre_stop_with_nothing_running_is_safe(self):
        self.assertEqual(self.run_hook("scripts/preStop.sh").returncode, 0)

    def test_restart_after_stop(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        self.run_hook("scripts/preStop.sh")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        self.assertEqual(self.supervisors(), 1)

    def test_callbacks_list(self):
        result = self.run_hook("callbacks", "--list")
        self.assertEqual(result.stdout.strip(), "lifecycle")

    def test_callbacks_ignore_other_lifecycle_events(self):
        self.assertEqual(self.run_hook("callbacks", "--type", "lifecycle", "shutdown").returncode, 0)
        time.sleep(0.3)
        self.assertEqual(self.supervisors(), 0)

    def test_uninstall_removes_everything_and_is_safe_twice(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        with open(self.paths.config_file, "w", encoding="utf-8") as handle:
            handle.write('radius = "5"\n')
        for _ in range(2):
            self.assertEqual(self.run_hook("scripts/fpp_uninstall.sh").returncode, 0)
        self.assertEqual(self.supervisors(), 0)
        self.assertFalse(os.path.exists(self.paths.data_dir))
        self.assertFalse(os.path.exists(self.paths.config_file))

    def test_reinstall_keeps_the_setup(self):
        self.run_hook("scripts/fpp_install.sh")
        with open(self.paths.config_file, "w", encoding="utf-8") as handle:
            handle.write('radius = "5"\n')
        self.run_hook("scripts/fpp_uninstall.sh", FPP_PLUGIN_REINSTALL="1")
        self.assertTrue(os.path.exists(self.paths.private_file))
        self.assertTrue(os.path.exists(self.paths.config_file))

    def test_only_an_exact_1_keeps_the_setup(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/fpp_uninstall.sh", FPP_PLUGIN_REINSTALL="0")
        self.assertFalse(os.path.exists(self.paths.data_dir))

    def test_any_other_reinstall_value_is_a_real_uninstall(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/fpp_uninstall.sh", FPP_PLUGIN_REINSTALL="2")
        self.assertFalse(os.path.exists(self.paths.data_dir))

    def test_only_one_log_is_created(self):
        self.run_hook("scripts/fpp_install.sh")
        self.run_hook("scripts/postStart.sh")
        self.assertTrue(self.wait_for(self.supervisor_running))
        self.assertTrue(self.wait_for(lambda: os.path.exists(self.paths.log_file)))
        self.assertEqual(os.listdir(os.path.join(self.media, "logs")), [f"plugin-{REPO}.log"])

    def test_uninstall_works_with_fppdir_unset(self):
        # uninstall_plugin passes FPPDIR only as an argument, and sudo strips the exported one,
        # so the script must fall back rather than fail. Here it falls back to /opt/fpp,
        # which doesn't exist, and that alone must not stop the script from finishing.
        env = {k: v for k, v in os.environ.items() if k != "FPPDIR"}
        result = subprocess.run(
            [os.path.join(self.plugin, "scripts/fpp_uninstall.sh")],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
