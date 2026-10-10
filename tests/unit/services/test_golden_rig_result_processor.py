
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
)

from stationapp.domain.errors import InvalidBatchTransition
from stationapp.domain.golden_rig import (
    GoldenRigProtocolError,
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
)

from stationapp.services.golden_rig_eligibility import (
    GoldenRigEligibilityService,
)

from stationapp.services.golden_rig_result_processor import (
    GoldenRigResultProcessor,
)

from stationapp.services.golden_rig_service import (
    GoldenRigInfrastructureError,
    GoldenRigTestReport,
)


def make_batch() -> Batch:
    return Batch(
        station_id="STATION-01",
        jig_id="JIG-8UP",
        slot_count=8,
        batch_id="BATCH-001",
    )


def prepare_slot(batch: Batch, number: int) -> None:
    slot = batch.slots[number]

    slot.bind_port(f"USB-SERIAL-{number:03d}")

    record = AllocatedMac(
        allocation_id=f"ALLOC-{number}",
        address=MacAddress.parse(
            f"AA:BB:CC:00:00:{number:02X}"
        ),
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    record.reserve(
        batch_id=batch.batch_id,
        slot_number=number,
    )

    slot.assign_stock_mac(record)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(record.address)


def make_partial_batch() -> Batch:
    batch = make_batch()

    for number in (1, 2, 4, 7):
        prepare_slot(batch, number)

    # A fixture representing the RF stage after
    # successful setup. The earlier partial-batch
    # aggregate transitions will be implemented in 9G.
    batch.state = BatchState.STOCK_RF_TESTING

    return batch


def make_report(
    batch: Batch,
    *,
    failure_slot: int | None = None,
    hold_slot: int | None = None,
    error_override: str | None = None,
) -> GoldenRigTestReport:

    targets = GoldenRigEligibilityService().build_targets(
        batch
    )

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

        if number == failure_slot:
            result = GoldenRigSlotResult(
                slot_number=number,
                expected_mac=target.expected_mac,
                reported_mac=None,
                connected=False,
                disconnected=False,
                error_code=(
                    error_override or "CONNECT_FAILED"
                ),
            )

        elif number == hold_slot:
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

    return GoldenRigTestReport(
        request=request,
        response=response,
    )

@pytest.mark.unit
def test_partial_jig_eligibility() -> None:
    batch = make_partial_batch()

    targets = GoldenRigEligibilityService().build_targets(
        batch
    )

    assert [
        target.slot_number
        for target in targets
    ] == [1, 2, 4, 7]


@pytest.mark.unit
def test_mixed_rf_results_do_not_stop_other_slots() -> None:
    batch = make_partial_batch()

    report = make_report(
        batch,
        failure_slot=2,
        hold_slot=7,
    )

    GoldenRigResultProcessor().apply(batch, report)

    assert (
        batch.slots[1].state
        is DeviceState.STOCK_RF_CONFIRMED
    )

    assert (
        batch.slots[4].state
        is DeviceState.STOCK_RF_CONFIRMED
    )

    assert batch.slots[2].state is DeviceState.HOLD
    assert batch.slots[7].state is DeviceState.HOLD

    assert batch.slots[1].stock_mac_record.status is MacStatus.CONFIRMED
    assert batch.slots[2].stock_mac_record.status is MacStatus.HOLD
    assert batch.slots[4].stock_mac_record.status is MacStatus.CONFIRMED
    assert batch.slots[7].stock_mac_record.status is MacStatus.HOLD

    assert batch.slots[3].state is DeviceState.EMPTY
    assert batch.slots[5].state is DeviceState.EMPTY
    assert batch.slots[6].state is DeviceState.EMPTY
    assert batch.slots[8].state is DeviceState.EMPTY

    assert batch.state is BatchState.STOCK_RF_CONFIRMED

@pytest.mark.unit
def test_infrastructure_failure_does_not_commit_dut_results() -> None:
    batch = make_partial_batch()

    report = make_report(
        batch,
        failure_slot=2,
        error_override="ADAPTER_ERROR",
    )

    with pytest.raises(GoldenRigInfrastructureError):
        GoldenRigResultProcessor().apply(batch, report)

    assert batch.state is BatchState.STOCK_RF_TESTING

    assert all(
        batch.slots[n].state
        is DeviceState.STOCK_READBACK_VERIFIED
        for n in (1, 2, 4, 7)
    )

    assert all(
        batch.slots[n].golden_rf_result is None
        for n in (1, 2, 4, 7)
    )


@pytest.mark.unit
def test_unknown_error_code_is_rejected() -> None:
    batch = make_partial_batch()

    report = make_report(
        batch,
        failure_slot=2,
        error_override="UNKNOWN_ERROR_123",
    )

    with pytest.raises(
        GoldenRigProtocolError,
        match="Unknown Golden Rig error code",
    ):
        GoldenRigResultProcessor().apply(batch, report)

    assert batch.state is BatchState.STOCK_RF_TESTING

    assert all(
        batch.slots[n].golden_rf_result is None
        for n in (1, 2, 4, 7)
    )

@pytest.mark.unit
def test_report_from_other_batch_rejected() -> None:
    batch = make_partial_batch()
    report = make_report(batch)

    other_batch = make_partial_batch()
    other_batch.batch_id = "BATCH-999"

    with pytest.raises(GoldenRigProtocolError):
        GoldenRigResultProcessor().apply(
            other_batch,
            report,
        )

    assert other_batch.state is BatchState.STOCK_RF_TESTING

@pytest.mark.unit
def test_same_rf_report_cannot_be_applied_twice() -> None:
    batch = make_partial_batch()
    report = make_report(batch)

    processor = GoldenRigResultProcessor()

    processor.apply(batch, report)

    with pytest.raises(InvalidBatchTransition):
        processor.apply(batch, report)

@pytest.mark.unit
def test_empty_jig_has_no_rf_targets() -> None:
    batch = make_batch()

    targets = GoldenRigEligibilityService().build_targets(
        batch
    )

    assert targets == ()


