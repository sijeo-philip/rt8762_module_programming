"""Environment verification for the Station App.

Run this after setting up the environment. It imports every dependency,
checks the Python version, and reports on the presence of external tools
and hardware. Nothing here touches the application code.

Usage:
    python scripts/verify_env.py
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (3, 12)

REQUIRED_MODULES = {
    "PyQt6": "GUI framework",
    "sqlalchemy": "ORM / database layer",
    "alembic": "schema migrations",
    "pydantic": "validation",
    "pydantic_settings": "validated configuration",
    "serial": "serial port access (pyserial)",
    "httpx": "LAN server client (Stage 2)",
    "argon2": "password hashing (argon2-cffi)",
    "openpyxl": "Excel report writing",
    "pytest": "test framework",
}


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check_python() -> bool:
    print("\n== Python ==")
    version = sys.version_info[:2]
    print(f"  interpreter : {sys.executable}")
    print(f"  version     : {sys.version.split()[0]}")

    passed = True
    if version < MIN_PYTHON:
        fail(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found {version[0]}.{version[1]}")
        passed = False
    elif version >= MAX_PYTHON_EXCLUSIVE:
        warn(f"Python {version[0]}.{version[1]} is newer than the tested target "
             f"(3.11). PyInstaller packaging may behave differently — confirm before shipping.")
    else:
        ok(f"Python version {version[0]}.{version[1]} is in the supported range")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        ok(f"running inside a virtual environment ({sys.prefix})")
    else:
        fail("NOT running inside a virtual environment — activate .venv first")
        passed = False

    return passed


def check_modules() -> bool:
    print("\n== Dependencies ==")
    passed = True
    for module_name, purpose in REQUIRED_MODULES.items():
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "unknown version")
            ok(f"{module_name:<18} {version:<12} ({purpose})")
        except ImportError as exc:
            fail(f"{module_name:<18} MISSING — {purpose} ({exc})")
            passed = False
    return passed


def check_qt() -> bool:
    print("\n== Qt ==")
    try:
        from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        ok(f"Qt {QT_VERSION_STR}, PyQt {PYQT_VERSION_STR}")
        return True
    except Exception as exc:  # noqa: BLE001 - report anything Qt throws at import
        fail(f"PyQt6 could not be imported: {exc}")
        return False


def check_external_tools() -> None:
    """External tools are reported, not failed — they may be absent on a dev PC."""
    print("\n== External tools ==")
    mpcli = os.environ.get("MPCLI_PATH", r"D:\BLE_Projects\StationApp\MPCliTool_v1.0.4.25_Windows\mpcli_v1.0.4.25_Windows\mpcli.exe")

    if Path(mpcli).is_file():
        ok(f"MpCli.exe found at {mpcli}")
        # Try the help output — this is also Lesson 7's first reconnaissance step.
        try:
            result = subprocess.run(
                [mpcli, "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            first_line = (result.stdout or result.stderr or "").strip().splitlines()
            preview = first_line[0] if first_line else "(no output)"
            ok(f"MpCli responded: {preview[:80]}")
        except subprocess.TimeoutExpired:
            warn("MpCli.exe did not respond within 15s")
        except Exception as exc:  # noqa: BLE001
            warn(f"MpCli.exe present but not runnable: {exc}")
    else:
        warn(f"MpCli.exe not found at {mpcli} "
             f"(set MPCLI_PATH in .env if it lives elsewhere)")

    bee = shutil.which("BeeMPTool.exe")
    if bee:
        ok(f"BeeMPTool.exe on PATH: {bee}")
    else:
        warn("BeeMPTool.exe not on PATH — expected; the operator launches it manually")


def check_serial_ports() -> None:
    print("\n== Serial ports ==")
    try:
        from serial.tools import list_ports
    except ImportError:
        fail("pyserial not available; cannot enumerate ports")
        return

    ports = list(list_ports.comports())
    if not ports:
        warn("no COM ports detected (expected on a dev PC with no jig attached)")
        return

    for port in sorted(ports, key=lambda p: p.device):
        print(f"  {port.device:<8} {port.description}")
    ok(f"{len(ports)} port(s) detected")

    print("\n  NOTE: COM port numbers depend on Windows enumeration order and")
    print("  change between boots. Lesson 8 will bind ports to panel positions")
    print("  at runtime, using the USB serial number — never a fixed COM number.")


def main() -> int:
    print("=" * 68)
    print(" Station App — environment verification")
    print("=" * 68)

    checks = [check_python(), check_modules(), check_qt()]

    check_external_tools()
    check_serial_ports()

    print("\n" + "=" * 68)
    if all(checks):
        print(" RESULT: environment is ready for Lesson 2.")
        return 0
    print(" RESULT: fix the [FAIL] items above before continuing.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
