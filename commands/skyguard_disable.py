#!/usr/bin/python3
"""FPP runs this directly, with almost no environment, and waits for it to exit."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skyguard import commands  # noqa: E402
from skyguard.config import Paths  # noqa: E402

commands.send_command(Paths.from_env(), commands.DISABLE)
