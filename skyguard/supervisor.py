"""Restarts the daemon if it dies, because a crash during CLEAR leaves the heads unprotected.

It never gives up, and backs off so a daemon that can't start doesn't spin. It holds no
state of its own: the ledger is what lets a restarted daemon pick up where the last left off.

Run with: python3 -m skyguard.supervisor run
          python3 -m skyguard.supervisor stop
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from .config import Paths
from .daemon import setup_logging
from .notifier import LOGGER_NAME

BACKOFF_S = (1, 2, 5, 10, 30)
HEALTHY_RUN_S = 60.0  # a run this long counts as healthy, so backoff starts over
CHILD_STOP_WAIT_S = 1.5
STOP_WAIT_S = 2.0
ORPHAN_WAIT_S = 0.2
STOP_POLL_S = 0.05
_SUPERVISOR = "skyguard.supervisor"
_DAEMON = "skyguard.daemon"

_logger = logging.getLogger(LOGGER_NAME)
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spawn_daemon(extra_args=()):
    return subprocess.Popen(
        [sys.executable, "-m", "skyguard.daemon", "--supervisor-pid", str(os.getpid()), *extra_args],
        cwd=_PLUGIN_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,  # the daemon writes its own log, which follows rotation
        stderr=subprocess.DEVNULL,
    )


class Supervisor:
    def __init__(self, spawn=spawn_daemon, clock=time.monotonic):
        self._spawn = spawn
        self._clock = clock
        self._stop = threading.Event()
        self._child = None

    def request_stop(self, *_args):
        self._stop.set()
        child = self._child
        if child is not None:
            try:
                child.terminate()  # the daemon releases its channels on the way out
            except OSError:
                pass

    def next_delay(self, failures):
        return BACKOFF_S[min(failures, len(BACKOFF_S) - 1)]

    def run(self):
        failures = 0
        while not self._stop.is_set():
            started = self._clock()
            try:
                self._child = self._spawn()
                code = self._child.wait()
            except OSError:
                _logger.exception("Could not start the daemon")
                code = None
            if self._stop.is_set():
                break
            if self._clock() - started >= HEALTHY_RUN_S:
                failures = 0
            delay = self.next_delay(failures)
            failures += 1
            _logger.warning("The daemon exited (code %s); restarting in %s s", code, delay)
            self._stop.wait(delay)
        self._finish_child()

    def _finish_child(self):
        child = self._child
        if child is None:
            return
        try:
            child.wait(CHILD_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            _logger.warning("The daemon didn't stop in time; killing it. Its channels stay held.")
            child.kill()


def _pid_is_ours(pid, proc_root="/proc", marker=_SUPERVISOR):
    """Check the process really is ours, so a reused PID is never signalled."""
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as handle:
            return marker.encode() in handle.read()
    except OSError:
        return False


def read_pid(path):
    try:
        with open(path, encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def is_running(paths, proc_root="/proc"):
    pid = read_pid(paths.supervisor_pid_file)
    return pid is not None and _pid_is_ours(pid, proc_root)


def _stop_pid(pid_file, marker, wait_s, proc_root, kill, clock, sleep):
    """SIGTERM a verified process, wait up to wait_s, then SIGKILL. Returns True if it was running."""
    pid = read_pid(pid_file)
    if pid is None or not _pid_is_ours(pid, proc_root, marker):
        _remove(pid_file)
        return False
    try:
        kill(pid, signal.SIGTERM)
    except OSError:
        _remove(pid_file)
        return False
    deadline = clock() + wait_s
    while clock() < deadline:
        if not _pid_is_ours(pid, proc_root, marker):
            _remove(pid_file)
            return True
        sleep(STOP_POLL_S)
    try:
        kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass
    _remove(pid_file)
    return True


def stop_running(paths, proc_root="/proc", kill=None, clock=time.monotonic, sleep=time.sleep):
    """Stop the supervisor and its daemon, waiting at most about 2 s. Safe if neither runs."""
    kill = os.kill if kill is None else kill
    stopped = _stop_pid(paths.supervisor_pid_file, _SUPERVISOR, STOP_WAIT_S, proc_root, kill, clock, sleep)
    # A supervisor that was killed can't have stopped its daemon, so check for one left behind.
    leftover = _stop_pid(paths.daemon_pid_file, _DAEMON, ORPHAN_WAIT_S, proc_root, kill, clock, sleep)
    return stopped or leftover


def stop_orphan_daemon(paths, proc_root="/proc", kill=None, clock=time.monotonic, sleep=time.sleep):
    """Two daemons would fight over the same ranges, so remove any left by a dead supervisor."""
    kill = os.kill if kill is None else kill
    if _stop_pid(paths.daemon_pid_file, _DAEMON, STOP_WAIT_S, proc_root, kill, clock, sleep):
        _logger.warning("Stopped a daemon left running by an earlier supervisor.")


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _take_single_instance_lock(path):
    """postStart.sh and the live-load callback can both start us. Returns None if another holds it.

    The lock goes when the process does, however it dies, so a crash can't block a restart.
    """
    try:
        import fcntl
    except ImportError:
        return True  # Windows, for development only
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a", encoding="ascii")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _write_pid(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as handle:
        handle.write(str(os.getpid()))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    action = argv[0] if argv else ""
    paths = Paths.from_env()
    if action == "stop":
        stop_running(paths)
        return 0
    if action != "run":
        print("usage: python3 -m skyguard.supervisor run|stop", file=sys.stderr)
        return 2

    setup_logging(paths)
    lock = _take_single_instance_lock(paths.supervisor_lock_file)
    if lock is None:
        return 0
    extra = tuple(argv[1:])  # passed to the daemon; tests use this for --fpp-url
    supervisor = Supervisor(spawn=lambda: spawn_daemon(extra))
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    _write_pid(paths.supervisor_pid_file)
    stop_orphan_daemon(paths)
    _logger.info("SkyGuard supervisor started")
    try:
        supervisor.run()
    finally:
        _remove(paths.supervisor_pid_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
