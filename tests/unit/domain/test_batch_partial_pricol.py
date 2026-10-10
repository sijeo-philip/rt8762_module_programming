
from __future__ import annotations

from uuid import uuid4

import pytest

from stationapp.domain import (
    AllocatedMac,
    Batch,
    BatchState,
    DeviceState,
    MacAddress,
    MacPurpose,
    MacStatus,
    VerificationMismatch,
)

from stationapp.domain.golden_rig import (
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
)

from stationapp.domain.errors import (
    InvalidBatchTransition,
)

from stationapp.services.golden_rig_eligibility import (
    GoldenRigEligibilityService,
)

from stationapp.services.golden_rig_result_processor import (
    GoldenRigResultProcessor,
)

from stationapp.services.golden_rig_service import (
    GoldenRigTestReport,
)


def make_stock_record(batch: Batch, number: int) -> AllocatedMac:
    record = AllocatedMac(
        allocation_id=f"STOCK-{number}",
        address=MacAddress.parse(
            f"AA:BB:CC:00:00:{number:02X}"
        ),
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    record.reserve(
        batch_id=batch.batch_id,
        slot_number=number,
    )
    return record


def make_pricol_record(batch: Batch, number: int) -> AllocatedMac:
    record = AllocatedMac(
        allocation_id=f"PRICOL-{number}",
        address=MacAddress.parse(
            f"DD:EE:FF:00:00:{number:02X}"
        ),
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )
    record.reserve(
        batch_id=batch.batch_id,
        slot_number=number,
    )
    return record


def make_rf_verified_batch() -> Batch:
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-8UP",
        slot_count=8,
        batch_id="BATCH-9G2-001",
    )
    batch.configure_occupied_slots((1, 2, 4, 7))

    # Prepare verified Stock MACs via real slot methods.
    for number in (1, 2, 4, 7):
        slot = batch.slots[number]

        slot.bind_port(f"USB-SERIAL-{number:03d}")
        record = make_stock_record(batch, number)

        slot.assign_stock_mac(record)
        slot.begin_stock_programming()
        slot.complete_stock_programming(True)
        slot.verify_stock_readback(record.address)

    batch.state = BatchState.STOCK_RF_TESTING

    targets = GoldenRigEligibilityService().build_targets(batch)

    request = GoldenRigRequest(
        request_id=str(uuid4()),
        batch_id=batch.batch_id,
        station_id=batch.station_id,
        jig_id=batch.jig_id,
        targets=targets,
    )

    results = []

    for target in targets:
        number = target.slot_number

        if number == 2:
            result = GoldenRigSlotResult(
                slot_number=number,
                expected_mac=target.expected_mac,
                reported_mac=None,
                connected=False,
                disconnected=False,
                error_code="CONNECT_FAILED",
            )

        elif number == 7:
            result = GoldenRigSlotResult(
                slot_number=number,
                expected_mac=target.expected_mac,
                reported_mac=target.expected_mac,
                connected=True,
                disconnected=False,
                error_code="DISCONNECT_FAILED",
            )

        else:
            result = GoldenRigSlotResult(
                slot_number=number,
                expected_mac=target.expected_mac,
                reported_mac=target.expected_mac,
                connected=True,
                disconnected=True,
            )

        results.append(result)

    response = GoldenRigResponse(
        request_id=request.request_id,
        rig_id="GOLDEN-RIG-01",
        results=tuple(results),
    )

    GoldenRigResultProcessor().apply(
        batch,
        GoldenRigTestReport(
            request=request,
            response=response,
        ),
    )

    assert batch.state is BatchState.STOCK_RF_CONFIRMED
    return batch


def reserve_pricol_candidates(batch: Batch) -> None:
    records = tuple(
        make_pricol_record(batch, number)
        for number in (1, 4)
    )
    batch.accept_pricol_reservations(records)


def begin_pricol_programming(batch: Batch) -> None:
    reserve_pricol_candidates(batch)
    batch.request_pricol_program_mode()
    batch.confirm_pricol_program_mode()

@pytest.mark.unit
def test_only_rf_pass_slots_are_pricol_eligible():
    batch = make_rf_verified_batch()

    assert tuple(
        slot.number
        for slot in batch.pricol_eligible_slots
    ) == (1, 4)

    assert batch.slots[2].state is DeviceState.HOLD
    assert batch.slots[7].state is DeviceState.HOLD

@pytest.mark.unit
def test_pricol_mac_allocation_only_for_passed_slots():
    batch = make_rf_verified_batch()

    reserve_pricol_candidates(batch)

    assert batch.state is BatchState.PRICOL_MACS_RESERVED

    assert batch.slots[1].pricol_mac_record is not None
    assert batch.slots[4].pricol_mac_record is not None

    for number in (2, 3, 5, 6, 7, 8):
        assert batch.slots[number].pricol_mac_record is None 

@pytest.mark.unit
def test_pricol_reservation_for_failed_slot_rejected():
    batch = make_rf_verified_batch()

    records = (
        make_pricol_record(batch, 1),
        make_pricol_record(batch, 2),
    )

    with pytest.raises(InvalidBatchTransition):
        batch.accept_pricol_reservations(records)

    # Reject the entire set before mutating valid slots.
    assert batch.slots[1].pricol_mac_record is None
    assert batch.slots[2].pricol_mac_record is None


@pytest.mark.unit
def test_pricol_programming_failure_isolated():
    batch = make_rf_verified_batch()

    begin_pricol_programming(batch)

    assert batch.pricol_readback_targets == ()

    batch.record_pricol_programming(
        1, succeeded=True
    )
    batch.record_pricol_programming(
        4, succeeded=False
    )

    assert batch.state is BatchState.PRICOL_PROGRAMMED

    assert batch.slots[1].state is DeviceState.PRICOL_PROGRAMMED
    assert batch.slots[4].state is DeviceState.HOLD

    assert batch.pricol_readback_targets == (1,)

    assert (
        batch.slots[4].pricol_mac_record.status
        is MacStatus.HOLD
    )

@pytest.mark.unit
def test_pricol_readback_failure_isolated():
    batch = make_rf_verified_batch()
    begin_pricol_programming(batch)

    batch.record_pricol_programming(1, succeeded=True)
    batch.record_pricol_programming(4, succeeded=True)

    batch.record_pricol_readback(
        1, str(batch.slots[1].pricol_mac)
    )

    with pytest.raises(VerificationMismatch):
        batch.record_pricol_readback(
            4, "DD:EE:FF:00:00:99"
        )

    assert (
        batch.state
        is BatchState.PRICOL_READBACK_VERIFIED
    )

    assert (
        batch.slots[1].state
        is DeviceState.PRICOL_MAC_CONFIRMED
    )

    assert batch.slots[4].state is DeviceState.HOLD

    assert (
        batch.slots[1].pricol_mac_record.status
        is MacStatus.CONFIRMED
    )

    assert (
        batch.slots[4].pricol_mac_record.status
        is MacStatus.HOLD
    )

@pytest.mark.unit
def test_all_pricol_programming_failures_hold_batch():
    batch = make_rf_verified_batch()
    begin_pricol_programming(batch)

    batch.record_pricol_programming(1, succeeded=False)
    batch.record_pricol_programming(4, succeeded=False)

    assert batch.state is BatchState.HOLD
    assert batch.slots[1].state is DeviceState.HOLD
    assert batch.slots[4].state is DeviceState.HOLD


@pytest.mark.unit
def test_pricol_readback_timeout_holds_only_affected_slot():
    batch = make_rf_verified_batch()
    begin_pricol_programming(batch)

    batch.record_pricol_programming(1, succeeded=True)
    batch.record_pricol_programming(4, succeeded=True)

    batch.record_pricol_readback_unverified(
        4,
        reason="MPCLI timeout",
    )

    assert batch.state is BatchState.PRICOL_PROGRAMMED

    batch.record_pricol_readback(
        1,
        str(batch.slots[1].pricol_mac),
    )

    assert (
        batch.state
        is BatchState.PRICOL_READBACK_VERIFIED
    )

    assert batch.slots[4].state is DeviceState.HOLD
    assert batch.slots[1].state is DeviceState.PRICOL_MAC_CONFIRMED

@pytest.mark.unit
def test_pricol_reservation_count_matches_rf_pass_count():
    batch = make_rf_verified_batch()

    assert batch.occupied_count == 4

    assert tuple(
        slot.number
        for slot in batch.pricol_eligible_slots
    ) == (1, 4)

    records = (
        make_pricol_record(batch, 1),
        make_pricol_record(batch, 4),
    )

    assert len(records) == 2

    batch.accept_pricol_reservations(records)

    assert batch.state is BatchState.PRICOL_MACS_RESERVED

    assert batch.slots[1].pricol_mac_record is not None
    assert batch.slots[4].pricol_mac_record is not None

    assert batch.slots[2].pricol_mac_record is None
    assert batch.slots[7].pricol_mac_record is None

