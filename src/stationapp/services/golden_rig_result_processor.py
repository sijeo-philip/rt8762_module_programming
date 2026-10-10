
"""Apply a validated Golden Rig report to physical DUT slots."""

from __future__ import annotations

from stationapp.domain.batch import Batch, BatchState
from stationapp.domain.errors import InvalidBatchTransition
from stationapp.domain.golden_rig import (
    GoldenRigOutcome,
    GoldenRigProtocolError,
)
from stationapp.domain.slot import DeviceState

from stationapp.services.golden_rig_eligibility import (
    GoldenRigEligibilityService,
)
from stationapp.services.golden_rig_service import (
    GoldenRigInfrastructureError,
    GoldenRigTestReport,
    INFRASTRUCTURE_ERROR_CODES,
)


# Conservative protocol-v1 device error vocabulary.
DUT_ERROR_CODES = frozenset({
    "CONNECT_FAILED",
    "DISCONNECT_FAILED",
    "ADDRESS_MISMATCH",
    "IDENTITY_UNCERTAIN",
    "TEST_INCOMPLETE",
})


class GoldenRigResultProcessor:
    """Convert complete RF evidence into per-slot state updates."""

    def __init__(self) -> None:
        self._eligibility = GoldenRigEligibilityService()

    def apply(self, batch: Batch, report: GoldenRigTestReport) -> None:

        if batch.state is not BatchState.STOCK_RF_TESTING:
            raise InvalidBatchTransition("Batch is not in Stock RF testing state")

        request = report.request
        response = report.response

        if (request.batch_id != batch.batch_id or request.station_id != batch.station_id or request.jig_id != batch.jig_id):
            raise GoldenRigProtocolError("RF report belongs to another batch or station")

        # Validate before modifying a single DUT.
        response.validate_against(request)

        expected_targets = self._eligibility.build_targets(batch)

        if set(request.targets) != set(expected_targets):
            raise GoldenRigProtocolError("RF targets do not match eligible jig slots")

        # Reject infrastructure and unknown error codes
        # before committing any individual result.
        for result in response.results:
            code = result.error_code

            if code in INFRASTRUCTURE_ERROR_CODES:
                raise GoldenRigInfrastructureError(f"Golden Rig infrastructure fault: {code}")

            if code is not None and code not in DUT_ERROR_CODES:
                raise GoldenRigProtocolError(f"Unknown Golden Rig error code: {code}")

            # A successful connection/disconnection without
            # a verified peer identity must never pass.
            if (result.outcome is GoldenRigOutcome.PASS and result.reported_mac != result.expected_mac):
                raise GoldenRigProtocolError("PASS has inconsistent MAC evidence")

        # All results are now structurally validated.
        # Each slot is updated independently.
        for result in response.results:
            slot = batch.slots[result.slot_number]
            slot.record_golden_rf_evidence(result)

        # Completion here means every submitted target
        # received a valid verdict, not that all DUTs passed.
        if any(slot.state is DeviceState.STOCK_RF_CONFIRMED for slot in batch.ordered_slots):
            batch.state = BatchState.STOCK_RF_CONFIRMED
        else:
            batch.place_on_hold("No modules passed Golden Rig RF testing")
