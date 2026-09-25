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

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
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

    return logging.getLogger("stationapp")
