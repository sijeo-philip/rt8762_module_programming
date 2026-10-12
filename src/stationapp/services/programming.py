"""Typed programming operations used by station orchestration.

This service is the boundary between manufacturing orchestration
and the Realtek MPCLI driver.

It contains no subprocess code and does not update persistence.
Database state transitions and manufacturing events belong to the
batch orchestrator / Unit of Work layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from stationapp.concurrency.cancellation import (
    CancellationToken,
)
from stationapp.domain.mac import (
    MacAddress,
    MacPurpose,
)
from stationapp.drivers.mpcli.configuration import (
    MpCliProgrammingProfile,
)
from stationapp.drivers.mpcli.driver import (
    MpCliDriver,
)
from stationapp.drivers.mpcli.parsers import (
    compare_readback,
)
from stationapp.drivers.mpcli.types import (
    IdentityVerificationResult,
    MacReadbackResult,
    ProgrammingResult,
)


class ProgrammingStage(str, Enum):
    """Firmware/MAC stage being performed."""

    STOCK = "STOCK"
    PRICOL = "PRICOL"


@dataclass(frozen=True, slots=True)
class ProgrammingTarget:
    """Everything needed to program one physical jig slot."""

    batch_id: str
    slot_number: int
    com_port: str
    mac: MacAddress
    purpose: MacPurpose

    def __post_init__(self) -> None:

        if not self.batch_id.strip():
            raise ValueError(
                "batch_id cannot be empty"
            )

        if self.slot_number <= 0:
            raise ValueError(
                "slot_number must be positive"
            )

        if not self.com_port.strip():
            raise ValueError(
                "com_port cannot be empty"
            )


@dataclass(frozen=True,slots=True)
class SlotProgrammingResult:
    """Programming result tied back to one physical slot."""
    target: ProgrammingTarget
    stage: ProgrammingStage
    programming: ProgrammingResult

@dataclass(frozen=True,slots=True)
class SlotReadbackResult:
    """Pricol MAC readback tied to one physical jig slot."""
    target: ProgrammingTarget
    readback: MacReadbackResult
    verification: IdentityVerificationResult


class ProgrammingService:
    """Station-facing programming operations.
    The service selects the correct firmware profile and validates
    that each MAC purpose is used only at its intended stage.
    """

    def __init__(self, *, driver: MpCliDriver, stock_profile: MpCliProgrammingProfile,
        pricol_profile: MpCliProgrammingProfile | None,
        programming_timeout_seconds: float = 60.0,
        readback_timeout_seconds: float = 10.0,
        two_step: bool = False) -> None:
        """two_step=True uses the validated flash -> settle -> write-MAC
        sequence instead of MPCLI's combined flash-with-MAC command."""

        if programming_timeout_seconds <= 0:
            raise ValueError(
                "programming_timeout_seconds must be positive"
            )

        if readback_timeout_seconds <= 0:
            raise ValueError(
                "readback_timeout_seconds must be positive"
            )

        self._driver = driver
        self._stock_profile = stock_profile
        self._pricol_profile = pricol_profile
        self._programming_timeout_seconds = (programming_timeout_seconds)
        self._readback_timeout_seconds = (readback_timeout_seconds)
        self._two_step = two_step

    def program_stock(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None) -> SlotProgrammingResult:
        """Flash stock firmware and temporary RF-test MAC."""

        self._require_purpose(
            target,
            MacPurpose.STOCK_RF_TEST,
        )
        result = self._program(target, self._stock_profile, cancellation_token, on_phase)

        return SlotProgrammingResult(
            target=target,
            stage=ProgrammingStage.STOCK,
            programming=result,
        )

    def program_pricol(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None) -> SlotProgrammingResult:
        """Flash Pricol firmware and permanent production MAC."""

        self._require_purpose(
            target,
            MacPurpose.PRICOL_PRODUCTION,
        )

        result = self._program(target, self._require_pricol_profile(), cancellation_token, on_phase)

        return SlotProgrammingResult(
            target=target,
            stage=ProgrammingStage.PRICOL,
            programming=result,
        )

    def read_stock_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> MacReadbackResult:
        """Read the device MAC after Stock programming."""

        self._require_purpose(target, MacPurpose.STOCK_RF_TEST)

        return self._driver.read_mac_structured(
            com_port=target.com_port,
            baud=self._stock_profile.baud,
            timeout_seconds=self._readback_timeout_seconds,
            cancellation_token=cancellation_token,
        )


    def verify_stock_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> SlotReadbackResult:
        """Read back and verify the temporary Stock RF-test MAC."""

        readback = self.read_stock_mac(target, cancellation_token=cancellation_token)
        verification = compare_readback(expected=target.mac, readback=readback)
        return SlotReadbackResult(target=target, readback=readback,verification=verification)

    def read_pricol_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> MacReadbackResult:
        """Read the device MAC after Pricol programming."""

        self._require_purpose(
            target,
            MacPurpose.PRICOL_PRODUCTION,
        )

        return self._driver.read_mac_structured(
            com_port=target.com_port,
            baud=self._require_pricol_profile().baud,
            timeout_seconds=(
                self._readback_timeout_seconds
            ),
            cancellation_token=cancellation_token,
        )

    def verify_pricol_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> SlotReadbackResult:
        """Read back and compare permanent production identity."""

        readback = self.read_pricol_mac(
            target,
            cancellation_token=cancellation_token,
        )

        verification = compare_readback(
            expected=target.mac,
            readback=readback,
        )

        return SlotReadbackResult(
            target=target,
            readback=readback,
            verification=verification,
        )

    def _program(self, target: ProgrammingTarget, profile: MpCliProgrammingProfile, cancellation_token: CancellationToken | None, on_phase: Callable[[str], None] | None) -> ProgrammingResult:
        if self._two_step:
            return self._driver.program_sequenced(
                com_port=target.com_port,
                mac=target.mac,
                profile=profile,
                timeout_seconds=self._programming_timeout_seconds,
                cancellation_token=cancellation_token,
                on_phase=on_phase,
            )

        return self._driver.program(
            com_port=target.com_port,
            mac=target.mac,
            profile=profile,
            timeout_seconds=self._programming_timeout_seconds,
            cancellation_token=cancellation_token,
        )

    def _require_pricol_profile(self) -> MpCliProgrammingProfile:
        if self._pricol_profile is None:
            raise ValueError("No Pricol firmware profile configured for this batch")
        return self._pricol_profile

    @staticmethod
    def _require_purpose(target: ProgrammingTarget, required: MacPurpose) -> None:
        if target.purpose is not required:
            raise ValueError(
                f"Slot {target.slot_number} MAC purpose is "
                f"{target.purpose.value}; "
                f"expected {required.value}"
            )

        