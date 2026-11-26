"""Logging Configuration.

Two Sinks, delibrately:
    * console - immediate feedback while developing
    * rotating file - the station PC's own record, independent of the
    database. The RFQ needs a durable local record that survives a database
    problem; this is the cheapest layer of that.

    Rotating rather than appending forever: a station doing 12500 transactions/data
    would otherwise fill a disk
    """

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stationapp.services.process_timing import TIMING_LOG_FILE, TIMING_LOGGER_NAME

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_TIMING_FORMAT = "%(asctime)s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s"

def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """ Configure root logging and return the application logger.
    Safe to call more than once: existing handlers are cleared first, so
    pytest and repeated app startups do not stack duplicate handlers.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "stationapp.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes = 5 * 1024 * 1024,
        backupCount = 10,
        encoding = "utf-8",
        )

    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    root.addHandler(file_handler)

    _setup_timing_log(log_dir)

    return logging.getLogger("stationapp")


def _setup_timing_log(log_dir: Path) -> None:
    """Process timings go to their own file only (stationapp_timing.log),
    not to the console or stationapp.log."""
    timing = logging.getLogger(TIMING_LOGGER_NAME)
    timing.setLevel(logging.INFO)
    timing.propagate = False
    for handler in list(timing.handlers):
        timing.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        log_dir / TIMING_LOG_FILE,
        maxBytes = 5 * 1024 * 1024,
        backupCount = 10,
        encoding = "utf-8",
        )
    handler.setFormatter(logging.Formatter(_TIMING_FORMAT))
    timing.addHandler(handler)
