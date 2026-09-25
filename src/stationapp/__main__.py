"""Application entry point.

Two ways in, both reaching main():
    python -n stationapp        (development, always works)
    stationapp                  (console script, after 'pip install -e .')

"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox
from stationapp.bootstrap import bootstrap

logger = logging.getLogger(__name__)

def main() -> int:
    """ Start the application. Return a process exit code."""
    # Bootstrap first -  before QApplication. Configuration errors must be
    # reported in the terminal and log file, not as a QMessageBox, because
    # at this point there may be no working display and the operator needs
    # the text for support.
    try:
        context = bootstrap()
    except Exception as exc: # noqa: BLE001 -startup failure must be visble
            print(f"FATAL: station app could not start: {exc}", file=sys.stderr)
            return 2

    app = QApplication(sys.argv)
    app.setApplicationName("Station App")
    app.setApplicationVersion(context.app_version)

    # Imported here, not at module top: importing UI pulls in Qt Widgets,
    # and we want a clean terminal error if bootstrap fails first
    from stationapp.ui import MainWindow

    window = MainWindow(context)
    window.show()
    logger.info("UI Started")
    exit_code = app.exec()
    logger.info("UI closed, exit code is %d", exit_code)
    return exit_code

if __name__ == "__main__":
        raise SystemExit(main())
