"""Entry point of the installed (PyInstaller) Station App.

The development entry point, ``python -m stationapp``, is unchanged. This
wrapper adds only what an installed, windowed build needs:

* The working directory becomes the install folder, so the relative paths
  in .env (./data, ./logs, ./config) resolve the same way they do in the
  repository, whichever way the app was started.
* A windowed build has no console. Startup errors that ``main()`` prints
  to stderr (for example an invalid .env) are written to
  logs/startup-errors.log and shown in a message box, so the operator
  can see them.
* Only one copy runs at a time: two copies would share one database and
  the same COM ports. The installer uses the same mutex name (AppMutex)
  so that it does not upgrade or uninstall a running app.
"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

MUTEX_NAME = "UnisemStationAppRunning"
TITLE = "Station App"
_ERROR_ALREADY_EXISTS = 183
_MB_ICONERROR = 0x10
_MB_ICONWARNING = 0x30


def _install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]  # repository root


def _message(text: str, icon: int = _MB_ICONERROR) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, icon)


class _ErrorCapture:
    """stderr replacement: appends to a log file and remembers the text."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file = None
        self.text = ""

    def write(self, data: str) -> int:
        self.text += data
        try:
            if self._file is None:
                self._file = self._path.open("a", encoding="utf-8")
                self._file.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            self._file.write(data)
            self._file.flush()
        except OSError:
            pass
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def main() -> int:
    app_dir = _install_dir()
    os.chdir(app_dir)

    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        _message("Station App is already running on this PC.", _MB_ICONWARNING)
        return 4

    log_dir = app_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    errors = _ErrorCapture(log_dir / "startup-errors.log")
    if getattr(sys, "frozen", False):
        # No console: stdout (the console log handler) is discarded, as the
        # same records already go to logs/stationapp.log.
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        sys.stderr = errors

    if not (app_dir / ".env").is_file():
        _message(
            f"The configuration file is missing:\n{app_dir / '.env'}\n\n"
            "Copy .env.example to .env and fill in STATION_ID, JIG_ID and the "
            "box settings, or run the installer again."
        )
        return 2

    try:
        from stationapp.__main__ import main as app_main

        code = app_main()
    except BaseException:  # noqa: BLE001 - must be visible to the operator
        traceback.print_exc()
        code = 1

    if code not in (0, 3):  # 3 = operator cancelled first-time setup
        detail = errors.text.strip()[-1500:] or f"Exit code {code}"
        _message(
            "Station App stopped with an error:\n\n"
            f"{detail}\n\n"
            f"Details: {log_dir / 'startup-errors.log'}\n"
            f"Application log: {log_dir / 'stationapp.log'}"
        )

    ctypes.windll.kernel32.CloseHandle(mutex)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
