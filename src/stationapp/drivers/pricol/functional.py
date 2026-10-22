"""Pricol firmware functional test over the DUT UART.

After every reset the Pricol firmware prints a start-up line

    ### Uart demo interrupt read uart data ###<CR><LF>

and only then accepts one command:

    PRICOLAPP<CR><LF>   jump to the main application
                        expected response: "PRICOLAPP"

    PRICOLDFU<CR><LF>   jump to Device Firmware Update mode
                        expected response: "PRICOLDFU"

So the station must be listening *before* the module is reset:

    1. the operator presses RUN APP TEST / RUN DFU TEST;
    2. the station opens the port and waits (reset_wait_seconds) for the
       start-up line - the operator resets / power-cycles the module now;
    3. as soon as the start-up line is complete, the command is sent;
    4. the expected response line must arrive within
       response_timeout_seconds.

A response passes only as a complete line, after removing any copy of the
start-up text printed on the same line (e.g. "PRICOLAPP### Uart demo
interrupt read uart data ###" passes the APP test). "PRICOLAPP" never
satisfies the DFU test.

Verdicts:
    True   the expected response line was received
    False  no start-up line (module not reset / not in application mode),
           or the DUT answered with something else, or stayed silent
    None   the station could not talk to the port (open/write/read
           failure): no trustworthy evidence, so the slot goes on HOLD
"""

from __future__ import annotations

import logging
import re
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

START_MESSAGE = "### Uart demo interrupt read uart data ###"

_MAX_CAPTURE = 4096
# A start-up message without a line ending is accepted after this pause.
_START_LINE_GRACE_SECONDS = 0.3


@dataclass(frozen=True, slots=True)
class FunctionalTestOutcome:
    """Verdict plus the raw evidence recorded in the manufacturing event."""

    passed: bool | None
    command: str
    expected: str
    response: str
    detail: str
    duration_seconds: float
    start_message: str = ""          # what the module printed after its reset


SessionFactory = Callable[[SerialSessionConfig], SerialSession]


def _default_session(config: SerialSessionConfig) -> SerialSession:
    return SerialSession(config=config)


def response_lines(raw: bytes) -> list[str]:
    """Split captured bytes into stripped lines; the last may be partial."""
    text = raw.decode("ascii", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _start_pattern(message: str) -> re.Pattern[str]:
    """Matches the start-up text with any surrounding '#' and spacing."""
    core = message.strip("# \t\r\n")
    return re.compile(r"#*\s*" + re.escape(core) + r"\s*#*", re.IGNORECASE)


class PricolFunctionalTester:
    """FunctionalTester implementation for real Pricol DUTs."""

    def __init__(
        self,
        *,
        baudrate: int = 115200,
        response_timeout_seconds: float = 3.0,
        session_factory: SessionFactory = _default_session,
        poll_seconds: float = 0.02,
        wait_for_start: bool = True,
        reset_wait_seconds: float = 30.0,
        start_message: str = START_MESSAGE,
        dtr: bool | None = False,
        rts: bool | None = False,
    ) -> None:
        if baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if response_timeout_seconds <= 0:
            raise ValueError("response_timeout_seconds must be positive")
        if reset_wait_seconds <= 0:
            raise ValueError("reset_wait_seconds must be positive")
        if not start_message.strip("# \t\r\n"):
            raise ValueError("start_message cannot be empty")
        self._baudrate = baudrate
        self._timeout = response_timeout_seconds
        self._session_factory = session_factory
        self._poll = poll_seconds
        self._wait_for_start = wait_for_start
        self._reset_wait = reset_wait_seconds
        self._start_message = start_message
        self._start = _start_pattern(start_message)
        self._dtr = dtr
        self._rts = rts

    @property
    def reset_wait_seconds(self) -> float:
        return self._reset_wait

    def run_app_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> FunctionalTestOutcome:
        return self._run(slot_number, com_port, APP_COMMAND, APP_RESPONSE, cancellation_token)

    def run_dfu_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> FunctionalTestOutcome:
        return self._run(slot_number, com_port, DFU_COMMAND, DFU_RESPONSE, cancellation_token)

    # ------------------------------------------------------------------

    def _matches(self, line: str, expected: str) -> bool:
        return self._start.sub("", line).strip() == expected

    def _start_complete(self, text: str, seen_at: float | None) -> tuple[bool, float | None]:
        """(start-up line complete?, time the start-up text was first seen)."""
        match = self._start.search(text)
        if match is None:
            return False, None
        seen_at = seen_at or time.monotonic()
        rest = text[match.end():]
        if "\n" in rest or "\r" in rest:
            return True, seen_at
        return time.monotonic() - seen_at >= _START_LINE_GRACE_SECONDS, seen_at

    def _run(self, slot_number: int, com_port: str, command: str, expected: str, token: CancellationToken | None) -> FunctionalTestOutcome:
        started = time.monotonic()
        captured = bytearray()
        start_text = ""

        def outcome(passed: bool | None, detail: str) -> FunctionalTestOutcome:
            result = FunctionalTestOutcome(
                passed=passed,
                command=command,
                expected=expected,
                response=captured.decode("ascii", errors="replace"),
                detail=detail,
                duration_seconds=round(time.monotonic() - started, 3),
                start_message=start_text,
            )
            logger.info(
                "Pricol %s | slot=%d port=%s passed=%s detail=%s start=%r response=%r",
                command, slot_number, com_port, passed, detail, start_text, result.response,
            )
            return result

        config = SerialSessionConfig(port=com_port, baudrate=self._baudrate, dtr=self._dtr, rts=self._rts)
        try:
            with self._session_factory(config) as session:
                # Anything printed before the station was listening is stale.
                session.reset_buffers()

                if self._wait_for_start:
                    # The operator resets the module now; send only once
                    # its start-up line is complete.
                    deadline = time.monotonic() + self._reset_wait
                    seen_at: float | None = None
                    ready = False
                    while time.monotonic() < deadline:
                        if token is not None:
                            token.raise_if_cancelled()
                        chunk = session.read_available()
                        if chunk:
                            captured.extend(chunk)
                            if len(captured) > _MAX_CAPTURE:
                                del captured[:-_MAX_CAPTURE]
                        ready, seen_at = self._start_complete(captured.decode("ascii", errors="replace"), seen_at)
                        if ready:
                            break
                        if not chunk:
                            time.sleep(self._poll)
                    start_text = captured.decode("ascii", errors="replace")
                    if not ready:
                        heard = f"; received instead: {start_text.strip()[:80]!r}" if start_text.strip() else ""
                        captured.clear()
                        return outcome(
                            False,
                            f"no start-up message {self._start_message!r} within {self._reset_wait:.0f}s - "
                            f"reset the module after pressing the button, in application mode{heard}",
                        )
                    captured.clear()

                session.write_line(command)

                deadline = time.monotonic() + self._timeout
                while time.monotonic() < deadline:
                    if token is not None:
                        token.raise_if_cancelled()

                    chunk = session.read_available()
                    if chunk:
                        captured.extend(chunk)
                        if any(self._matches(line, expected) for line in response_lines(bytes(captured))):
                            return outcome(True, "expected response received")
                        if len(captured) >= _MAX_CAPTURE:
                            break
                    else:
                        time.sleep(self._poll)

        except SerialDriverError as exc:
            return outcome(None, f"serial communication failed: {exc}")

        if not captured:
            return outcome(False, f"no response within {self._timeout:.1f}s after sending {command}")
        return outcome(False, "unexpected response")
