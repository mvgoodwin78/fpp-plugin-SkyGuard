"""The supervisor's logic, and real processes being started, killed and restarted."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from skyguard.config import Paths
from skyguard.supervisor import (
    BACKOFF_S,
    HEALTHY_RUN_S,
    Supervisor,
    _pid_is_ours,
    _take_single_instance_lock,
    is_running,
    read_pid,
    stop_orphan_daemon,
    stop_running,
)
from tests.fake_fpp_server import FakeFpp
from tests.mock_feed_server import MockFeedServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSIDE = {"lat": 40.05, "lon": -75.0}
PLANE = {"hex": "a1", "alt_baro": 3000, "gs": 120.0, "seen_pos": 0.5, **INSIDE}


class SingleInstanceTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "file locks need Linux")
    def test_a_second_holder_is_refused_until_the_first_goes(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "sub", "supervisor.lock")
        first = _take_single_instance_lock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(_take_single_instance_lock(path))
        first.close()
        second = _take_single_instance_lock(path)
        self.assertIsNotNone(second)
        second.close()


class FakeChild:
    def __init__(self, code=1, on_wait=None):
        self.code = code
        self.on_wait = on_wait
        self.terminated = False
        self.killed = False
        self.ignores_terminate = False

    def wait(self, timeout=None):
        if timeout is not None and self.ignores_terminate:
            raise subprocess.TimeoutExpired("daemon", timeout)
        if self.on_wait:
            self.on_wait()
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class SupervisorLogicTests(unittest.TestCase):
    def setUp(self):
        self.time = 0.0
        self.delays = []
        self.spawned = 0

    def supervisor(self, children=None, run_seconds=0.0, stop_after=5):
        def spawn():
            self.spawned += 1
            if self.spawned >= stop_after:
                sup.request_stop()
            self.time += run_seconds
            return (children or {}).get(self.spawned, FakeChild())

        sup = Supervisor(spawn=spawn, clock=lambda: self.time)
        real_wait = sup._stop.wait

        def fake_wait(delay=None):
            self.delays.append(delay)
            return real_wait(0)

        sup._stop.wait = fake_wait
        return sup

    def run_quietly(self, sup):
        with self.assertLogs("skyguard", level="WARNING"):
            sup.run()

    def test_restarts_the_daemon_every_time_it_exits(self):
        self.run_quietly(self.supervisor(stop_after=5))
        self.assertEqual(self.spawned, 5)

    def test_backoff_grows_and_is_capped(self):
        self.run_quietly(self.supervisor(stop_after=8))
        self.assertEqual(self.delays, [1, 2, 5, 10, 30, 30, 30])

    def test_a_clean_exit_is_restarted_too(self):
        # Only an explicit stop ends the supervisor, or the heads would be left unprotected.
        children = {n: FakeChild(code=0) for n in range(1, 4)}
        self.run_quietly(self.supervisor(children, stop_after=3))
        self.assertEqual(self.spawned, 3)

    def test_a_long_healthy_run_resets_the_backoff(self):
        self.run_quietly(self.supervisor(run_seconds=HEALTHY_RUN_S, stop_after=4))
        self.assertEqual(self.delays, [BACKOFF_S[0]] * 3)

    def test_a_failure_to_start_is_retried(self):
        calls = []

        def spawn():
            calls.append(1)
            if len(calls) == 3:
                sup.request_stop()
            raise OSError("no such file")

        sup = Supervisor(spawn=spawn, clock=lambda: self.time)
        sup._stop.wait = lambda delay=None: sup._stop.is_set()
        with self.assertLogs("skyguard", level="ERROR"):
            sup.run()
        self.assertEqual(len(calls), 3)

    def test_each_restart_is_logged(self):
        with self.assertLogs("skyguard", level="WARNING") as captured:
            self.supervisor(stop_after=3).run()
        self.assertEqual(sum("restarting" in line for line in captured.output), 2)

    def test_stop_terminates_the_child_so_it_can_release(self):
        sup = Supervisor(spawn=FakeChild)
        child = FakeChild()
        sup._child = child
        sup.request_stop()
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)

    def test_a_child_ignoring_terminate_is_killed(self):
        sup = Supervisor(spawn=FakeChild)
        child = FakeChild()
        child.ignores_terminate = True
        sup._child = child
        with self.assertLogs("skyguard", level="WARNING"):
            sup._finish_child()
        self.assertTrue(child.killed)

    def test_stop_before_any_child_is_safe(self):
        Supervisor(spawn=FakeChild).request_stop()


class FakeProc:
    """A stand-in for /proc, so PID checks can run on any system."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()

    def add(self, pid, cmdline):
        os.makedirs(os.path.join(self.dir, str(pid)), exist_ok=True)
        with open(os.path.join(self.dir, str(pid), "cmdline"), "wb") as handle:
            handle.write(cmdline)

    def remove(self, pid):
        shutil.rmtree(os.path.join(self.dir, str(pid)), ignore_errors=True)


class PidTests(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        self.addCleanup(shutil.rmtree, self.proc.dir, ignore_errors=True)
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.paths = Paths.from_env({"MEDIADIR": self.media})
        os.makedirs(self.paths.data_dir)
        self.signals = []

    def write_pid(self, text="1234"):
        with open(self.paths.supervisor_pid_file, "w", encoding="ascii") as handle:
            handle.write(text)

    def stop(self, kill=None, clock=None, sleep=None):
        def default_kill(pid, sig):
            self.signals.append((pid, sig))
            self.proc.remove(pid)

        return stop_running(
            self.paths,
            proc_root=self.proc.dir,
            kill=kill or default_kill,
            clock=clock or time.monotonic,
            sleep=sleep or (lambda s: None),
        )

    def test_recognizes_our_process(self):
        self.proc.add(1234, b"python3\0-m\0skyguard.supervisor\0run\0")
        self.assertTrue(_pid_is_ours(1234, self.proc.dir))

    def test_a_reused_pid_is_not_ours(self):
        self.proc.add(1234, b"/usr/bin/fppd\0")
        self.assertFalse(_pid_is_ours(1234, self.proc.dir))

    def test_a_missing_process_is_not_ours(self):
        self.assertFalse(_pid_is_ours(1234, self.proc.dir))

    def test_read_pid_handles_bad_files(self):
        self.assertIsNone(read_pid(self.paths.supervisor_pid_file))
        self.write_pid("not a number")
        self.assertIsNone(read_pid(self.paths.supervisor_pid_file))

    def test_is_running(self):
        self.assertFalse(is_running(self.paths, self.proc.dir))
        self.write_pid()
        self.proc.add(1234, b"python3\0-m\0skyguard.supervisor\0run\0")
        self.assertTrue(is_running(self.paths, self.proc.dir))

    def test_stop_signals_our_process(self):
        self.write_pid()
        self.proc.add(1234, b"python3\0-m\0skyguard.supervisor\0run\0")
        self.assertTrue(self.stop())
        self.assertEqual(self.signals, [(1234, signal.SIGTERM)])
        self.assertFalse(os.path.exists(self.paths.supervisor_pid_file))

    def test_stop_never_signals_a_reused_pid(self):
        self.write_pid()
        self.proc.add(1234, b"/usr/bin/fppd\0")
        self.assertFalse(self.stop())
        self.assertEqual(self.signals, [])

    def test_stop_with_nothing_running_is_safe_twice(self):
        self.assertFalse(self.stop())
        self.assertFalse(self.stop())

    def write_daemon_pid(self, text="5678"):
        with open(self.paths.daemon_pid_file, "w", encoding="ascii") as handle:
            handle.write(text)

    def test_stop_also_ends_a_daemon_left_behind(self):
        self.write_daemon_pid()
        self.proc.add(5678, b"python3\0-m\0skyguard.daemon\0")
        self.assertTrue(self.stop())
        self.assertEqual(self.signals, [(5678, signal.SIGTERM)])
        self.assertFalse(os.path.exists(self.paths.daemon_pid_file))

    def test_stop_never_signals_a_reused_daemon_pid(self):
        self.write_daemon_pid()
        self.proc.add(5678, b"/usr/bin/fppd\0")
        self.assertFalse(self.stop())
        self.assertEqual(self.signals, [])

    def test_a_supervisor_pid_is_not_mistaken_for_a_daemon(self):
        self.write_daemon_pid()
        self.proc.add(5678, b"python3\0-m\0skyguard.supervisor\0run\0")
        self.assertFalse(self.stop())
        self.assertEqual(self.signals, [])

    def test_starting_removes_an_orphan_daemon(self):
        self.write_daemon_pid()
        self.proc.add(5678, b"python3\0-m\0skyguard.daemon\0")

        def kill(pid, sig):
            self.signals.append((pid, sig))
            self.proc.remove(pid)

        with self.assertLogs("skyguard", level="WARNING"):
            stop_orphan_daemon(self.paths, proc_root=self.proc.dir, kill=kill, sleep=lambda s: None)
        self.assertEqual(self.signals, [(5678, signal.SIGTERM)])

    def test_starting_with_no_orphan_signals_nothing(self):
        stop_orphan_daemon(self.paths, proc_root=self.proc.dir, kill=lambda *a: self.fail("signalled"))

    def test_stop_gives_up_waiting_after_about_two_seconds_and_kills(self):
        self.write_pid()
        self.proc.add(1234, b"python3\0-m\0skyguard.supervisor\0run\0")
        now = [0.0]

        def advance(seconds):
            now[0] += seconds

        def stubborn(pid, sig):
            self.signals.append(sig)

        self.assertTrue(self.stop(kill=stubborn, clock=lambda: now[0], sleep=advance))
        self.assertEqual(len(self.signals), 2)
        self.assertLessEqual(now[0], 2.1)


def wait_until(condition, timeout_s=15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class RealProcessTestCase(unittest.TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.paths = Paths.from_env({"MEDIADIR": self.media})
        os.makedirs(os.path.dirname(self.paths.config_file))
        os.makedirs(self.paths.data_dir)

        self.fpp = FakeFpp()
        self.addCleanup(self.fpp.stop)
        self.fpp.status = {"status": 1, "status_name": "playing"}
        self.fpp.universes = {
            "channelOutputs": [
                {
                    "type": "universes",
                    "enabled": 1,
                    "universes": [{"active": 1, "id": 1, "startChannel": 1, "channelCount": 512}],
                }
            ]
        }

        self.feed = MockFeedServer().start()
        self.addCleanup(self.feed.stop)
        self.route = self.feed.add("/data/aircraft.json", [{"aircraft": [PLANE]}])

        with open(self.paths.config_file, "w", encoding="utf-8") as handle:
            handle.write(f'source_mode = "receiver"\nreceiver_url = "{self.feed.url("/data/aircraft.json")}"\n')
            handle.write('release_delay_s = "1"\nradius_receiver = "10"\npersonal_use_confirmed = "1"\n')
        with open(self.paths.private_file, "w", encoding="utf-8") as handle:
            json.dump({"center_lat": 40.0, "center_lon": -75.0}, handle)
        with open(self.paths.fixtures_file, "w", encoding="utf-8") as handle:
            json.dump([{"name": "A", "universe": 1, "dimmer_ch": 10}], handle)

        self.env = dict(os.environ, MEDIADIR=self.media, PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("LOGDIR", None)
        self.processes = []
        self.addCleanup(self.kill_all)

    def start(self, *args):
        process = subprocess.Popen(
            [sys.executable, "-m", *args],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        return process

    def kill_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(5)

    def status(self):
        try:
            with open(self.paths.status_file, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def log(self):
        try:
            with open(self.paths.log_file, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""


class RealDaemonTests(RealProcessTestCase):
    def daemon(self):
        return self.start("skyguard.daemon", "--fpp-url", self.fpp.url)

    def test_a_hard_kill_leaves_the_heads_dark_and_a_restart_recovers(self):
        first = self.daemon()
        self.assertTrue(wait_until(lambda: self.fpp.ranges.get(10) == 0), self.log())
        self.assertTrue(wait_until(lambda: self.status().get("reason") == "aircraft"), self.log())

        first.kill()
        first.wait(5)
        time.sleep(0.5)
        self.assertEqual(self.fpp.output(10), 0)  # still dark with no daemon running

        self.route.docs = [{"aircraft": []}]
        self.daemon()
        self.assertTrue(wait_until(lambda: self.fpp.output(10) == 100), self.log())
        self.assertIn("Resuming in blackout", self.log())
        self.assertEqual(self.fpp.ever_full_on, [])

    def test_the_log_is_the_one_expected_file(self):
        self.daemon()
        self.assertTrue(wait_until(lambda: "daemon started" in self.log()), self.log())
        logs = os.path.join(self.media, "logs")
        self.assertEqual(os.listdir(logs), ["plugin-fpp-plugin-SkyGuard.log"])


class RealSupervisorTests(RealProcessTestCase):
    def test_a_killed_daemon_is_restarted_and_recovers(self):
        self.start("skyguard.supervisor", "run", "--fpp-url", self.fpp.url)
        self.assertTrue(wait_until(lambda: self.fpp.ranges.get(10) == 0), self.log())

        pid = self.daemon_pid()
        self.assertIsNotNone(pid, self.log())
        self.kill_pid(pid)
        self.assertTrue(wait_until(lambda: "restarting" in self.log()), self.log())
        self.assertEqual(self.fpp.output(10), 0)

        self.route.docs = [{"aircraft": []}]
        self.assertTrue(wait_until(lambda: self.fpp.output(10) == 100), self.log())
        self.assertIn("Resuming in blackout", self.log())
        self.assertEqual(self.fpp.ever_full_on, [])
        self.kill_pid(self.daemon_pid())

    def daemon_pid(self):
        wait_until(lambda: os.path.exists(self.paths.daemon_pid_file))
        return read_pid(self.paths.daemon_pid_file)

    def kill_pid(self, pid):
        if pid is None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    @unittest.skipIf(os.name == "nt", "Windows can't deliver SIGTERM for a graceful stop")
    def test_stop_releases_the_channels(self):
        self.start("skyguard.supervisor", "run", "--fpp-url", self.fpp.url)
        self.assertTrue(wait_until(lambda: self.fpp.ranges.get(10) == 0), self.log())
        stop_running(self.paths)
        self.assertTrue(wait_until(lambda: 10 not in self.fpp.ranges), self.log())
        self.assertEqual(self.fpp.ever_full_on, [])


if __name__ == "__main__":
    unittest.main()
