"""Releases everything the ledger says is held. Used on uninstall, after the daemon has stopped.

Run with: python3 -m skyguard.release
"""

import argparse
import sys
import time

from .config import Paths
from .daemon import setup_logging
from .fpp_api import BASE_URL, FppApi
from .matrix import MatrixController
from .notifier import Notifier
from .overlay import Ledger, OverlayController


def release_everything(paths, api, clock=time.monotonic):
    """Returns True if every channel was released. Safe to run when nothing is held."""
    ledger = Ledger(paths.ledger_file)
    released = OverlayController(api, ledger, Notifier(), clock).release_all()
    MatrixController(api, ledger, clock).clear_all()
    return released and not ledger.matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description="Release SkyGuard's channels")
    parser.add_argument("--fpp-url", default=BASE_URL, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)
    paths = Paths.from_env()
    setup_logging(paths)
    return 0 if release_everything(paths, FppApi(args.fpp_url)) else 1


if __name__ == "__main__":
    sys.exit(main())
