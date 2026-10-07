""" Unit tests for the health checks.

These run with no database, no jig, and no MpCli -- which is exactly the point
of the layered design. The checks take Settings as an argument rather than
reading a global, so a test constructs any station it likes.
"""

from __future__ import annotations
from pathlib import Path
import pytest

from stationapp.config import Settings
from stationapp.services.health import(
    HealthCheck,
    Severity,
    check_configuration,
    check_log_directory,
    check_mpcli,
    summarise
)

def _settings(tmp_path: Path, **overrides) -> Settings:
    """Build a Settings instance for tests, bypassing .env entirely."""
    base = {
        "station_id": "TEST-01",
        "jig_id": "JIG-01",
        "jig_positions": 8,
        "log_dir": tmp_path / "logs",
        "mpcli_path": tmp_path / "MpCli.exe",
    }

    base.update(overrides)
    return Settings(**base)
