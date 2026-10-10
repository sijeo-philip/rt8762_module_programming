
"""Golden Module Rig RF-test orchestration service.

The service:
- uses a supervisor-approved authenticated transport;
- exchanges Golden Rig JSON protocol messages;
- validates complete response correlation and coverage;
- separates DUT-level outcomes from rig infrastructure faults.

It does not mutate Batch or JigSlot states.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from stationapp.domain.golden_rig import (
    GoldenRigOutcome,
    GoldenRigProtocolError,
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)

from stationapp.domain.mac import MacAddress

from stationapp.infrastructure.golden_rig.codec import (GoldenRigJsonCodec)
from stationapp.infrastructure.golden_rig.transport import (GoldenRigTransport)


# These error codes indicate that the rig could not
# produce trustworthy DUT-level RF-test evidence.
#
# The Golden Rig server must use the same vocabulary.
INFRASTRUCTURE_ERROR_CODES = frozenset({
    "SERVER_UNAVAILABLE",
    "ADAPTER_ERROR",
    "ADAPTER_UNAVAILABLE",
    "RIG_BUSY",
    "RIG_INTERNAL_ERROR",
    "SERVER_INTERNAL_ERROR",
    "BLUETOOTH_STACK_ERROR",
    "TEST_ENGINE_ERROR",
})


class GoldenRigInfrastructureError(RuntimeError):
    """Golden Rig reported an infrastructure-level failure."""


class ApprovedGoldenRigTransport(Protocol):
    """Transport carrying the same approved binding used for TLS."""

    @property
    def binding(self):
        ...

    def exchange(self, payload: bytes, *, cancel_event: threading.Event | None = None) -> bytes:
        ...


class GoldenRigTransportProvider(Protocol):
    """Factory that enforces the approved Golden Rig binding."""

    def create_transport(self) -> ApprovedGoldenRigTransport:
        ...


@dataclass(frozen=True, slots=True)
class GoldenRigTestReport:
    """Validated RF-test evidence for one request."""

    request: GoldenRigRequest
    response: GoldenRigResponse

    @property
    def passed_slots(self) -> tuple[int, ...]:
        return tuple(
            result.slot_number
            for result in self.response.results
            if result.outcome is GoldenRigOutcome.PASS
        )

    @property
    def failed_slots(self) -> tuple[int, ...]:
        return tuple(
            result.slot_number
            for result in self.response.results
            if result.outcome is GoldenRigOutcome.FAIL
        )

    @property
    def held_slots(self) -> tuple[int, ...]:
        return tuple(
            result.slot_number
            for result in self.response.results
            if result.outcome is GoldenRigOutcome.HOLD
        )

    @property
    def all_passed(self) -> bool:
        return self.response.all_passed

    def result_for_slot(self, slot_number: int) -> GoldenRigSlotResult:
        for result in self.response.results:
            if result.slot_number == slot_number:
                return result

        raise KeyError(f"Slot {slot_number} was not part of this RF test")


class GoldenRigService:
    """Run a complete authenticated RF-test request."""

    def __init__(self, transport_provider: GoldenRigTransportProvider) -> None:
        self._transport_provider = transport_provider

    def run_test(self, *, batch_id: str, station_id: str, jig_id: str, targets: tuple[GoldenRigTarget, ...],cancel_event: threading.Event | None = None) -> GoldenRigTestReport:

        # Do not connect if there is nothing eligible to test.
        # Request validation also rejects duplicate slots/MACs.
        if not targets:
            raise GoldenRigProtocolError("No eligible Stock MACs for RF testing")

        request = GoldenRigRequest(
            request_id=str(uuid4()),
            batch_id=batch_id,
            station_id=station_id,
            jig_id=jig_id,
            targets=targets,
        )

        # The provider must load an enabled,
        # supervisor-approved binding and construct
        # SecureGoldenRigTransport from that binding.
        transport = self._transport_provider.create_transport()
        binding = transport.binding
        if binding.station_id != station_id:
            raise GoldenRigProtocolError("Approved rig binding belongs to another station")

        binding.require_station(station_id)

        # The request is encoded before network transmission.
        request_bytes = GoldenRigJsonCodec.encode_request(request)

        response_bytes = transport.exchange(request_bytes, cancel_event=cancel_event)

        # A response is not accepted until ALL entries
        # have passed protocol, identity, and coverage checks.
        response = GoldenRigJsonCodec.decode_response(
            response_bytes,
            request=request,
            approved_rig_id=binding.rig_id,
        )

        self._check_infrastructure_faults(response)

        return GoldenRigTestReport(request=request, response=response)

    @staticmethod
    def _check_infrastructure_faults(response: GoldenRigResponse) -> None:
        for result in response.results:
            if result.error_code in INFRASTRUCTURE_ERROR_CODES:
                raise GoldenRigInfrastructureError(
                    "Golden Rig infrastructure error "
                    f"at slot {result.slot_number}: "
                    f"{result.error_code}"
                )

    @staticmethod
    def build_targets(eligible_slots: tuple[tuple[int, MacAddress], ...]) -> tuple[GoldenRigTarget, ...]:
        """Convert prevalidated slot identities into RF targets."""

        return tuple(
            GoldenRigTarget(
                slot_number=slot_number,
                expected_mac=mac,
            )
            for slot_number, mac in eligible_slots
        )
