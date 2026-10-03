"""Typed values returned by the MPCLI infrastructure."""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class ProcessOutcome(str, Enum):
    """Low-level outcome of invoking MPCLI."""

    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    START_FAILED = "START_FAILED"
    TERMINATED = "TERMINATED"

@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Raw result from one MPCLI process invocation."""
    command: tuple[str, ...]
    outcome: ProcessOutcome
    return_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def completed(self) -> bool:
        return (
            self.outcome
            is ProcessOutcome.COMPLETED
        )

    @property
    def succeeded(self) -> bool:
        return (
            self.completed
            and self.return_code == 0
        )

from stationapp.domain.mac import MacAddress


class ProgrammingOutcome(str, Enum):
    """Manufacturing interpretation of one programming attempt."""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class ProgrammingResult:
    """Structured interpretation of one MPCLI programming attempt.

    SUCCESS means MPCLI completed the requested programming command.

    It does NOT mean that the final MAC identity has independently
    passed read-back or RF verification.
    """

    outcome: ProgrammingOutcome
    process: ProcessResult
    message: str

    @property
    def succeeded(self) -> bool:
        return (
            self.outcome
            is ProgrammingOutcome.SUCCESS
        )

    @property
    def uncertain(self) -> bool:
        return (
            self.outcome
            is ProgrammingOutcome.UNCERTAIN
        )


class MacReadbackOutcome(str, Enum):
    """Interpretation of an MPCLI MAC read-back attempt."""

    READ = "READ"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    PROCESS_FAILED = "PROCESS_FAILED"
    UNCERTAIN = "UNCERTAIN"

@dataclass(frozen=True, slots=True)
class MacReadbackResult:
    """MAC extracted from one MPCLI read-back operation."""

    outcome: MacReadbackOutcome
    process: ProcessResult
    mac: MacAddress | None
    message: str

    @property
    def readable(self) -> bool:
        return (
            self.outcome
            is MacReadbackOutcome.READ
            and self.mac is not None
        )

    def matches(self, expected: MacAddress) -> bool:
        """Return True only for a definite matching read-back."""

        return (
            self.readable
            and self.mac == expected
        )

class IdentityOutcome(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_VERIFIED = "NOT_VERIFIED"


@dataclass(frozen=True, slots=True)
class IdentityVerificationResult:
    """Comparison of allocated versus device-reported MAC."""
    outcome: IdentityOutcome
    expected: MacAddress
    reported: MacAddress | None
    message: str

    @property
    def matched(self) -> bool:
        return (
            self.outcome
            is IdentityOutcome.MATCH
        )   