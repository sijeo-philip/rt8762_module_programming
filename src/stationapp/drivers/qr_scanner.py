"""USB QR / barcode scanner (keyboard-wedge HID) input handling.

The station's scanners are USB HID "keyboard wedge" devices: the
scanner types what it reads into whichever field has focus. There is no
COM port. The driver logic is therefore the normalisation of that typed
input, carried over from the legacy station tools (Client_8jig_qr,
Pricol_qr_App):

    1. keep only hexadecimal characters, upper-cased;
    2. keep the LAST `length` of them (module QR labels: 8);
    3. a scan is complete when that many characters are present.

The legacy tools fired at the moment the 8th character arrived, which
could split longer labels depending on timing. Here completion is
decided once the scanner's burst of keystrokes has settled (see
ScannerLineEdit), so the result is always the last 8 hex characters.

Badges use a variable-length format: any printable characters, complete
on the scanner's Enter suffix or when its burst ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_HEX = re.compile(r"[^0-9A-Fa-f]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InvalidScan(ValueError):
    """Scanned data does not form a complete code."""


@dataclass(frozen=True, slots=True)
class ScanFormat:
    """How raw scanner input becomes a code.

    length:     exact number of characters kept (last N); 0 = variable.
    hex_only:   discard everything except 0-9 / A-F and upper-case.
    min_length: smallest acceptable variable-length code.
    """

    length: int = 8
    hex_only: bool = True
    min_length: int = 3

    def __post_init__(self) -> None:
        if self.length < 0 or self.min_length < 1:
            raise ValueError("Invalid scan format")

    def normalise(self, raw: str) -> str:
        if self.hex_only:
            text = _NON_HEX.sub("", raw).upper()
        else:
            text = _CONTROL.sub("", raw).strip()
        if self.length:
            text = text[-self.length:]
        return text

    def is_complete(self, code: str) -> bool:
        if self.length:
            return len(code) == self.length
        return len(code) >= self.min_length

    def validate(self, raw: str) -> str:
        """Normalised, complete code - or InvalidScan."""
        code = self.normalise(raw)
        if not self.is_complete(code):
            raise InvalidScan(f"Invalid QR data {raw.strip()!r}: expected {self.description}")
        return code

    @property
    def description(self) -> str:
        if self.length and self.hex_only:
            return f"{self.length} hexadecimal characters"
        if self.length:
            return f"{self.length} characters"
        return f"at least {self.min_length} characters"


class ScanDebouncer:
    """Discard repeated reads of the same code.

    Scanners can deliver one label twice (double trigger, long press,
    continuous-scan mode). A code identical to the previous accepted
    code, arriving within `window_s` of the last read of it, is
    discarded. Each repeat restarts the window, so a held trigger never
    produces a second record. Only *consecutive* repeats are dropped:
    A, B, A accepts both A reads.
    """

    def __init__(self, window_s: float = 2.0, clock=None) -> None:
        import time

        if window_s < 0:
            raise ValueError("window_s cannot be negative")
        self._window = window_s
        self._clock = clock or time.monotonic
        self._last_code: str | None = None
        self._last_seen = 0.0

    def accept(self, code: str) -> bool:
        now = self._clock()
        repeat = (
            self._window > 0
            and code == self._last_code
            and now - self._last_seen < self._window
        )
        self._last_code = code
        self._last_seen = now
        return not repeat

    def forget(self) -> None:
        """Accept the next read even if it repeats the last code
        (e.g. the previous scan was refused and must be retried)."""
        self._last_code = None


# Module QR labels, as in the legacy tools (QR_LEN = 8, hex only).
MODULE_QR_FORMAT = ScanFormat(length=8, hex_only=True)

# Operator / Supervisor badges: free text, ends with Enter or the burst.
BADGE_FORMAT = ScanFormat(length=0, hex_only=False, min_length=3)
