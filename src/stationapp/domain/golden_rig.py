
"""Golden Module Rig protocol domain contracts.

This module contains immutable, transport-independent objects.

It does not:
- open sockets;
- perform Bluetooth operations;
- modify batch states;
- allocate MAC addresses;
- perform JSON serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from stationapp.domain.mac import MacAddress


GOLDEN_RIG_PROTOCOL_VERSION = 1


class GoldenRigProtocolError(ValueError):
    """Invalid Golden Rig request or response contract."""


class GoldenRigOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class GoldenRigTarget:
    """One eligible physical jig position."""

    slot_number: int
    expected_mac: MacAddress

    def __post_init__(self) -> None:
        if (isinstance(self.slot_number, bool) or not isinstance(self.slot_number, int) or self.slot_number <= 0):
            raise GoldenRigProtocolError("slot_number must be a positive integer")

        if not isinstance(self.expected_mac, MacAddress):
            raise GoldenRigProtocolError("expected_mac must be a MacAddress")


@dataclass(frozen=True, slots=True)
class GoldenRigRequest:
    """One RF-test request sent to the Golden Rig."""

    request_id: str
    batch_id: str
    station_id: str
    jig_id: str
    targets: tuple[GoldenRigTarget, ...]
    protocol_version: int = GOLDEN_RIG_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != GOLDEN_RIG_PROTOCOL_VERSION:
            raise GoldenRigProtocolError("Unsupported protocol version")

        try:
            UUID(self.request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise GoldenRigProtocolError("request_id must be a UUID") from exc

        for name in ("batch_id", "station_id", "jig_id"):
            value = getattr(self, name)

            if not isinstance(value, str) or not value.strip():
                raise GoldenRigProtocolError(f"{name} must be non-empty")

        if not isinstance(self.targets, tuple):
            raise GoldenRigProtocolError("targets must be a tuple")

        if not 1 <= len(self.targets) <= 8:
            raise GoldenRigProtocolError("An RF request requires 1 to 8 targets")

        if not all(isinstance(item, GoldenRigTarget) for item in self.targets):
            raise GoldenRigProtocolError("Invalid RF target")

        slots = [item.slot_number for item in self.targets]
        macs = [item.expected_mac for item in self.targets]

        if len(slots) != len(set(slots)):
            raise GoldenRigProtocolError("Duplicate slot in RF request")

        if len(macs) != len(set(macs)):
            raise GoldenRigProtocolError("Duplicate MAC in RF request")


@dataclass(frozen=True, slots=True)
class GoldenRigSlotResult:
    """Bluetooth observations reported for one DUT."""

    slot_number: int
    expected_mac: MacAddress
    reported_mac: MacAddress | None
    connected: bool
    disconnected: bool
    error_code: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if (isinstance(self.slot_number, bool) or not isinstance(self.slot_number, int) or self.slot_number <= 0):
            raise GoldenRigProtocolError("Invalid result slot number")

        if not isinstance(self.expected_mac, MacAddress):
            raise GoldenRigProtocolError("Invalid expected MAC")

        if (self.reported_mac is not None and not isinstance(self.reported_mac, MacAddress)):
            raise GoldenRigProtocolError("Invalid reported MAC")

        if not isinstance(self.connected, bool):
            raise GoldenRigProtocolError("connected must be Boolean")

        if not isinstance(self.disconnected, bool):
            raise GoldenRigProtocolError("disconnected must be Boolean")

        if self.disconnected and not self.connected:
            raise GoldenRigProtocolError("Cannot disconnect without connecting")

        if self.error_code is not None:
            if (not isinstance(self.error_code, str) or not self.error_code.strip()):
                raise GoldenRigProtocolError("error_code must be non-empty or None")

        if not isinstance(self.detail, str):
            raise GoldenRigProtocolError("detail must be a string")

    @property
    def outcome(self) -> GoldenRigOutcome:
        """Classify device evidence, not transport health."""

        if (self.reported_mac is not None and self.reported_mac != self.expected_mac):
            return GoldenRigOutcome.HOLD

        if self.connected and self.disconnected:
            if (self.reported_mac == self.expected_mac and self.error_code is None):
                return GoldenRigOutcome.PASS

        if self.connected and not self.disconnected:
            return GoldenRigOutcome.HOLD

        if self.error_code in {"IDENTITY_UNCERTAIN", "ADDRESS_MISMATCH", "TEST_INCOMPLETE"}:
            return GoldenRigOutcome.HOLD

        return GoldenRigOutcome.FAIL


@dataclass(frozen=True, slots=True)
class GoldenRigResponse:
    """Complete response from one Golden Rig operation."""

    request_id: str
    rig_id: str
    results: tuple[GoldenRigSlotResult, ...]
    protocol_version: int = GOLDEN_RIG_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != GOLDEN_RIG_PROTOCOL_VERSION:
            raise GoldenRigProtocolError("Unsupported response protocol version")

        try:
            UUID(self.request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise GoldenRigProtocolError("Invalid response request_id") from exc

        if not isinstance(self.rig_id, str) or not self.rig_id.strip():
            raise GoldenRigProtocolError("rig_id must be non-empty")

        if not isinstance(self.results, tuple):
            raise GoldenRigProtocolError("results must be a tuple")

        if not 1 <= len(self.results) <= 8:
            raise GoldenRigProtocolError("Response must contain 1 to 8 results")

        if not all(isinstance(item, GoldenRigSlotResult) for item in self.results):
            raise GoldenRigProtocolError("Invalid RF result")
        slots = [item.slot_number for item in self.results]
        if len(slots) != len(set(slots)):
            raise GoldenRigProtocolError("Duplicate slot in RF response")

    def validate_against(self, request: GoldenRigRequest) -> None:
        """Validate correlation, coverage and assigned identity."""

        if self.request_id != request.request_id:
            raise GoldenRigProtocolError("RF response request ID mismatch")

        if self.protocol_version != request.protocol_version:
            raise GoldenRigProtocolError("RF protocol version mismatch")

        expected = {item.slot_number: item.expected_mac for item in request.targets}
        received = {item.slot_number: item.expected_mac for item in self.results}

        if expected != received:
            raise GoldenRigProtocolError("RF response slot or expected MAC mismatch")

    @property
    def all_passed(self) -> bool:
        return all(item.outcome is GoldenRigOutcome.PASS for item in self.results)
