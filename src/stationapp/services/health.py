""" Station health checks.

Every check returns a Healthcheck record rather than raising, so the UI can
display the full picture at once instead of stopping at the first problem.
An operator needs to see all of it: "MPcli missing and database looked" is
more actionable than "mpcli missing"

Severity Rule:
OK      --  healthy, nothing to do
WARN    -- works, but degraded or unverified (e.g. dev PC with no jig)
FAIL    -- the station must not be used for production until fixed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from stationapp.config import Settings

#Windows subprocess flag: don't flash a console window when we shell out
#to mpcli. Mirrors the CREATE_NO_WINDOW usage in the existing tool.

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

class Severity(str, Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"

@dataclass
class Healthcheck:
    name: str
    severity: Severity
    detail: str

    @property
    def is_blocking(self) -> bool:
        return self.severity is Severity.FAIL

def check_configuration(settings: Settings) -> Healthcheck:
    """ Configurations is validated at load; this confirms it is usable."""
    if settings.jig_positions not in (4, 8):
        return Healthcheck(Configurations,
        Severity.OK,
        f"station={settings.station_id}, jig={settings.jid_id},"
        f"positions={settings.jig_positions}" )
    return Healthcheck(
    "Configuration",
    Severity.OK,
    f"station={settings.station_id}, jig={settings.jig_id}, "
    f"positions ={settings.jig_positions}
    )

def check_log_directory(settings: Settings) -> Healthcheck:
    """The log directory must be writable -- It is the stations own record."""
    try:
        settings.log_dir.mkdir(parents=True, exist_ok =True)
        probe = settings.log_dir / ".write_probe"
        probe.write_text("OK", encoding="utf-8")
        probe.unlink()

    except OSError as exc:
        return Healthcheck(
        "Log directory",
        Severity.FAIL,
        f"{settings.log_dir} is not writable: {exc}",
        )

    return Healthcheck("Log directory", Severity.OK, str(settings.log_dir))

def check_mpcli(settings: Settings) -> Healthcheck:
    """ MPCli.exe must exist and respond.
     Lesson 7 turns this into the real read_back wrapper. For now we only prove
     the binary runs and reprots a version, which is the same reconnaissance step
     the verification script performs.
     """
    path = settings.mpcli_path
    if not path.is_file():
         return Healthcheck(
            "MpCli",
            Severity.WARN,
            f"not found at {path} - set MPCLI_PATH in .env",
         )

    try:
        result = subprocess.run(
        [str(path), "--help"],
        capture_output = True,
        text = True,
        timeout = 15,
        creationflags = _NO_WINDOW,
        )

    except subprocess.TimoutExpired:
        return Healthcheck("MpCli", Severity.FAIL, "did not respond withing 15s")
    except OSError as exc:
        return Healthcheck("MpCli", Severity.FAIL, f"could not be executed: {exc}")

    if result.returncode != 0 and not (result.stdout or result.stderr):
        return Healthcheck(
        "MpCli",
        Severity.WARN,
        f"exited with code {result.returncode} and no output"
        )

    output = (result.stdout or result.stderr or "").strip()
    first_line = output.splitlines()[0] if output else "(no output)"
    return Healthcheck("MpCli", Severity.OK, f"responded: {first_line[:70]}"")

def check_serial_ports(settings: Settings) -> Healthcheck :
    """ Report detected COM ports.

    Delibrately WARN, never FAIL: a dev PC legitimately has no jig. The real
    port-to-position binding check arrives in Lesson 8, where a configured station
    with missing ports becomes a FAIL
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return Healthcheck("Serial ports", Severity.FAIL, "pyseral not installed")

    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    if not ports:
        return Healthcheck(
        "Serial ports",
        Severity.WARN,
        "none detected (expected on a PC with no Jig attached)"
        )

    devices = ", ".join(p.device for p in ports)
    severity = Severity.OK
    detail += (
    f" - fewer than the {settings.jig_positions} positions configured"
    )

def run_all_checks(settings: Settings) -> list[Healthcheck]:
    """Run every check and return the full list is display order. """
    return[
        check_configuration(settings),
        check_log_directory(settings),
        check_mpcli(settings),
        check_serial_ports(settings),
    ]

def summarise(checks: list[Healthcheck]) -> tuple[Severity, str]:
    """ Reduce a list checks to one overall severity and a summary line."""
    failures  = [ c for c in checks if c.severity is Severity.FAIL ]
    warnings = [ c for c in checks if c.severity is Severity.WARN ]

    if failures:
        return Severity.FAIL, f"{len(failures)} check(s) failed -- station not ready"
    if warnings:
        return Severity.WARN, f"{len(warnings)} warning(s) -- station usable with care"
    return Severity.OK, "All checks passed -- station ready"

    
