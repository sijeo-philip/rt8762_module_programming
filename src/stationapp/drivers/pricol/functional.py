"""Pricol firmware functional test over the DUT UART.

After each physical reset the Pricol bootloader accepts one command:

    PRICOLAPP<CR><LF>   jump to the main application
                        expected response: "PRICOLAPP"

    PRICOLDFU<CR><LF>   jump to Device Firmware Update mode
                        expected response: "PRICOLDFU"

Responses confirmed on a real DUT with scripts/test_pricol_functional.py.
A response passes only as a complete line (surrounding whitespace and
CR/LF ignored), so e.g. "PRICOLAPP" never satisfies the DFU test.

The operator performs the reset (the workflow prompts for it) and the
station then sends the command on each eligible slot's COM port in
parallel.

Verdicts:
    True   the expected response line was received
    False  the DUT answered with something else, or stayed silent
    None   the station could not talk to the port (open/write/read
           failure): no trustworthy evidence, so the slot goes on HOLD
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from stationapp.concurrency.cancellation import CancellationToken
from stationapp.drivers.serial.errors import SerialDriverError
from stationapp.drivers.serial.session import SerialSession, SerialSessionConfig

logger = logging.getLogger(__name__)

APP_COMMAND = "PRICOLAPP"
APP_RESPONSE = "PRICOLAPP"

DFU_COMMAND = "PRICOLDFU"
DFU_RESPONSE = "PRICOLDFU"

_MAX_CAPTURE = 4096


@dataclass(frozen=True, slots=True)
class FunctionalTestOutcome:
    """Verdict plus the raw evidence recorded in the manufacturing event."""

    passed: bool | None
    command: str
    expected: str
    response: str
    detail: str
    duration_seconds: float


SessionFactory = Callable[[SerialSessionConfig], SerialSession]


def _default_session(config: SerialSessionConfig) -> SerialSession:
    return SerialSession(config=config)


def response_lines(raw: bytes) -> list[str]:
    """Split captured bytes into stripped lines; the last may be partial."""
    text = raw.decode("ascii", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


class PricolFunctionalTester:
    """FunctionalTester implementation for real Pricol DUTs."""

    def __init__(self, *, baudrate: int = 115200, response_timeout_seconds: float = 3.0, session_factory: SessionFactory = _default_session, poll_seconds: float = 0.02) -> None:
        if baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if response_timeout_seconds <= 0:
            raise ValueError("response_timeout_seconds must be positive")
        self._baudrate = baudrate
        self._timeout = response_timeout_seconds
        self._session_factory = session_factory
        self._poll = poll_seconds

    def run_app_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> FunctionalTestOutcome:
        return self._run(slot_number, com_port, APP_COMMAND, APP_RESPONSE, cancellation_token)

    def run_dfu_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> FunctionalTestOutcome:
        return self._run(slot_number, com_port, DFU_COMMAND, DFU_RESPONSE, cancellation_token)

    def _run(self, slot_number: int, com_port: str, command: str, expected: str, token: CancellationToken | None) -> FunctionalTestOutcome:
        started = time.monotonic()
        captured = bytearray()

        def outcome(passed: bool | None, detail: str) -> FunctionalTestOutcome:
            result = FunctionalTestOutcome(
                passed=passed,
                command=command,
                expected=expected,
                response=captured.decode("ascii", errors="replace"),
                detail=detail,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            logger.info(
                "Pricol %s | slot=%d port=%s passed=%s detail=%s response=%r",
                command, slot_number, com_port, passed, detail, result.response,
            )
            return result

        config = SerialSessionConfig(port=com_port, baudrate=self._baudrate)
        try:
            with self._session_factory(config) as session:
                # Discard boot noise emitted by the reset before the command.
                session.reset_buffers()
                session.write_line(command)

                deadline = time.monotonic() + self._timeout
                while time.monotonic() < deadline:
                    if token is not None:
                        token.raise_if_cancelled()

                    chunk = session.read_available()
                    if chunk:
                        captured.extend(chunk)
                        if any(line == expected for line in response_lines(bytes(captured))):
                            return outcome(True, "expected response received")
                        if len(captured) >= _MAX_CAPTURE:
                            break
                    else:
                        time.sleep(self._poll)

        except SerialDriverError as exc:
            return outcome(None, f"serial communication failed: {exc}")

        if not captured:
            return outcome(False, f"no response within {self._timeout:.1f}s")
        return outcome(False, "unexpected response")
