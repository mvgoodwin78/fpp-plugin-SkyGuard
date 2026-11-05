"""How FPP command scripts reach the daemon: a spool folder of small JSON files.

A spool opens no port and needs no listener, so nothing new is reachable on the
network. A script only drops a file and exits, which fppd needs because it waits
for command scripts. Enable and Disable also survive a daemon outage this way.
"""

import json
import logging
import os
import stat
import time
import uuid

from .notifier import LOGGER_NAME
from .overlay import atomic_write_json

ENABLE = "enable"
DISABLE = "disable"
TEST = "test"
KNOWN_COMMANDS = (ENABLE, DISABLE, TEST)

DEFAULT_TEST_SECONDS = 10
MAX_TEST_SECONDS = 3600
TEST_EXPIRY_S = 30.0  # a test requested long ago, while the daemon was down, must not fire now
MAX_COMMANDS_PER_READ = 100
MAX_COMMAND_BYTES = 4096

_logger = logging.getLogger(LOGGER_NAME)


def send_command(paths, name, seconds=None):
    """Queue a command for the daemon. Returns quickly."""
    if name not in KNOWN_COMMANDS:
        raise ValueError(f"unknown command: {name}")
    doc = {"command": name, "at": time.time()}
    if seconds is not None:
        doc["seconds"] = seconds
    # Time first, so sorting by name is the order they were sent.
    filename = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
    atomic_write_json(os.path.join(paths.commands_dir, filename), doc)


def test_seconds(value):
    """A usable test length from whatever the command was given, or the default."""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_TEST_SECONDS
    return seconds if 1 <= seconds <= MAX_TEST_SECONDS else DEFAULT_TEST_SECONDS


def _read_one(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_COMMAND_BYTES:
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_pending(paths, wall_clock=time.time):
    """Return queued commands, oldest first, and remove them. Bad files are removed and logged."""
    try:
        names = sorted(n for n in os.listdir(paths.commands_dir) if n.endswith(".json"))
    except OSError:
        return []

    commands = []
    for name in names[:MAX_COMMANDS_PER_READ]:
        path = os.path.join(paths.commands_dir, name)
        try:
            doc = _read_one(path)
        except (OSError, ValueError):
            doc = None
        try:
            os.unlink(path)
        except OSError:
            _logger.warning("Could not remove a command file.")

        if not isinstance(doc, dict) or doc.get("command") not in KNOWN_COMMANDS:
            _logger.warning("Ignored an unreadable command file.")
            continue
        if doc["command"] == TEST:
            sent_at = doc.get("at")
            if not isinstance(sent_at, (int, float)) or abs(wall_clock() - sent_at) > TEST_EXPIRY_S:
                _logger.info("Ignored an expired test command.")
                continue
        commands.append(doc)
    return commands
