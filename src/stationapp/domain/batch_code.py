"""Batch Code: the production batch every module is reported with.

The box (SDK 7.0) requires a batch ID on every reported module. On the
station it is called the Batch Code, because a StationApp "batch" is one
tray.

* The Supervisor sets the threshold (passed modules per Batch Code) when
  starting the production session.
* The Operator enters the starting series once per session, e.g. S5A01.
* Each Batch Code holds exactly `threshold` passed modules. Passes are
  counted only when a tray is committed (the whole cycle finished),
  in ascending slot order, so one tray may straddle two codes. The next
  code is the trailing number plus one, keeping its zero padding:
  S5A09 -> S5A10, S5A99 -> S5A100.
* Failed modules are reported with the code current at their slot and
  are not counted. Aborted trays are never committed, so never counted.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

MAX_LENGTH = 64            # box rule: 1 to 64 printable ASCII characters
MAX_THRESHOLD = 1_000_000

_PRINTABLE = re.compile(r"^[\x20-\x7E]{1,64}$")
_TRAILING_DIGITS = re.compile(r"^(.*?)(\d+)$")


class BatchCodeError(ValueError):
    """A Batch Code or threshold breaks the rules; the message is operator-facing."""


def validate_batch_code(text: str) -> str:
    """The code as the box will record it, or BatchCodeError."""
    code = (text or "").strip()
    if not code:
        raise BatchCodeError("Enter the Batch Code")
    if not _PRINTABLE.fullmatch(code):
        raise BatchCodeError(
            f"The Batch Code must be 1 to {MAX_LENGTH} characters: letters, digits, spaces or punctuation"
        )
    if _TRAILING_DIGITS.fullmatch(code) is None:
        raise BatchCodeError(f"The Batch Code must end in a number so it can go up by one (e.g. {code}01)")
    return code


def validate_threshold(value: int) -> int:
    try:
        threshold = int(value)
    except (TypeError, ValueError) as exc:
        raise BatchCodeError("The Batch Code threshold must be a whole number") from exc
    if not 1 <= threshold <= MAX_THRESHOLD:
        raise BatchCodeError(f"The Batch Code threshold must be between 1 and {MAX_THRESHOLD}")
    return threshold


def next_batch_code(code: str) -> str:
    """S5A01 -> S5A02, S5A09 -> S5A10, S5A99 -> S5A100."""
    match = _TRAILING_DIGITS.fullmatch(code)
    if match is None:
        raise BatchCodeError(f"Batch Code {code!r} does not end in a number")
    prefix, digits = match.groups()
    following = f"{prefix}{int(digits) + 1:0{len(digits)}d}"
    if len(following) > MAX_LENGTH:
        raise BatchCodeError(f"Batch Code {code!r} cannot go up by one: the next code is longer than {MAX_LENGTH} characters")
    return following


@dataclass(frozen=True, slots=True)
class BatchCodeState:
    """The running Batch Code of one production session."""

    session_id: str
    threshold: int
    code: str | None = None          # None until the Operator enters the series
    passed: int = 0                  # passed modules already on `code`
    series_start: str | None = None

    def __post_init__(self) -> None:
        validate_threshold(self.threshold)
        if self.code is not None:
            validate_batch_code(self.code)
        if not 0 <= self.passed <= self.threshold:
            raise BatchCodeError(f"Passed count {self.passed} is outside 0..{self.threshold}")

    @property
    def ready(self) -> bool:
        return self.code is not None

    @property
    def remaining(self) -> int:
        """Passed modules still to go on the current code."""
        return self.threshold - self.passed

    def started(self, code: str) -> "BatchCodeState":
        """A new series: the code given, nothing counted on it yet."""
        code = validate_batch_code(code)
        return replace(self, code=code, passed=0, series_start=code)

    def allocate(self, passed: Sequence[bool]) -> tuple[tuple[str, ...], "BatchCodeState"]:
        """Batch Codes for one committed tray's modules, in slot order.

        passed[i] tells whether module i passed. Returns one code per
        module and the state after the tray. A code moves on only when a
        pass would exceed the threshold, so each code holds exactly
        `threshold` passes and a full code is still used for failures
        that follow its last pass.
        """
        if self.code is None:
            raise BatchCodeError("No Batch Code series has been entered")
        code, count = self.code, self.passed
        codes = []
        for ok in passed:
            if ok:
                if count >= self.threshold:
                    code, count = next_batch_code(code), 0
                count += 1
            codes.append(code)
        return tuple(codes), replace(self, code=code, passed=count)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "threshold": self.threshold,
            "code": self.code,
            "passed": self.passed,
            "series_start": self.series_start,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchCodeState":
        return cls(
            session_id=str(data["session_id"]),
            threshold=int(data["threshold"]),
            code=data.get("code") or None,
            passed=int(data.get("passed", 0)),
            series_start=data.get("series_start") or None,
        )
