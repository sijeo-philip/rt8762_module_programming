from __future__ import annotations

import pytest

from stationapp.domain import (
    AllocatedMac,
    Batch,
    BatchOnHold,
    BatchState,
    DeviceState,
    DuplicateModuleQr,
    InvalidBatchTransition,
    MacAddress,
    MacPurpose,
    VerificationMismatch,
)


def make_batch(slot_count: int = 4) -> Batch:
    return Batch(
        station_id="STN-01",
        jig_id="JIG-01",
        slot_count=slot_count,
        batch_id="BATCH-001",
    )


def reserve_records(
    batch: Batch,
    *,
    purpose: MacPurpose,
    prefix: str,
) -> tuple[AllocatedMac, ...]:
    records: list[AllocatedMac] = []

    for slot in batch.ordered_slots:
        record = AllocatedMac(
            allocation_id=f"ALLOC-{purpose.value}",
            address=MacAddress.parse(
                f"{prefix}:{slot.number:02X}"
            ),
            purpose=purpose,
        )
        record.reserve(
            batch_id=batch.batch_id,
            slot_number=slot.number,
        )
        records.append(record)

    return tuple(records)


def bind_batch_ports(batch: Batch) -> None:
    batch.begin_port_binding()

    for number in range(1, batch.slot_count + 1):
        batch.bind_port(
            number,
            f"USB-SERIAL-{number:03d}",
        )

    assert batch.state is BatchState.PORTS_BOUND


def advance_to_stock_rf(batch: Batch) -> None:
    bind_batch_ports(batch)

    stock_records = reserve_records(
        batch,
        purpose=MacPurpose.STOCK_RF_TEST,
        prefix="AA:BB:CC:00:00",
    )
    batch.accept_stock_reservations(stock_records)
    batch.request_stock_program_mode()
    batch.confirm_stock_program_mode()

    for slot in batch.ordered_slots:
        batch.record_stock_programming(
            slot.number,
            succeeded=True,
        )

    assert batch.state is BatchState.STOCK_PROGRAMMED

    for slot in batch.ordered_slots:
        assert slot.stock_mac is not None

        batch.record_stock_readback(slot.number, str(slot.stock_mac))

    assert batch.state is BatchState.STOCK_READBACK_VERIFIED

    batch.request_stock_rf_mode()
    batch.begin_stock_rf_test()


def advance_to_pricol_readback(batch: Batch) -> None:
    advance_to_stock_rf(batch)

    for slot in batch.ordered_slots:
        assert slot.stock_mac is not None
        batch.record_stock_rf_result(
            slot.number,
            str(slot.stock_mac),
        )

    pricol_records = reserve_records(
        batch,
        purpose=MacPurpose.PRICOL_PRODUCTION,
        prefix="DD:EE:FF:00:00",
    )
    batch.accept_pricol_reservations(pricol_records)
    batch.request_pricol_program_mode()
    batch.confirm_pricol_program_mode()

    for slot in batch.ordered_slots:
        batch.record_pricol_programming(
            slot.number,
            succeeded=True,
        )


def advance_to_functional_app_test(batch: Batch) -> None:
    advance_to_pricol_readback(batch)

    for slot in batch.ordered_slots:
        assert slot.pricol_mac is not None
        batch.record_pricol_readback(
            slot.number,
            str(slot.pricol_mac),
        )

    assert batch.state is BatchState.PRICOL_READBACK_VERIFIED

    batch.request_functional_test_mode()
    batch.confirm_functional_test_mode()
    batch.confirm_pricol_app_reset()




@pytest.mark.unit
def test_stock_reservations_require_known_modules_and_ports() -> None:
    batch = make_batch()
    bind_batch_ports(batch)

    records = reserve_records(
        batch,
        purpose=MacPurpose.STOCK_RF_TEST,
        prefix="AA:BB:CC:00:00",
    )
    batch.accept_stock_reservations(records)

    assert batch.state is BatchState.STOCK_MACS_RESERVED
    assert all(
        slot.stock_mac is not None
        for slot in batch.ordered_slots
    )


@pytest.mark.unit
def test_stock_programming_failure_holds_batch() -> None:
    batch = make_batch()
    bind_batch_ports(batch)

    records = reserve_records(
        batch,
        purpose=MacPurpose.STOCK_RF_TEST,
        prefix="AA:BB:CC:00:00",
    )
    batch.accept_stock_reservations(records)
    batch.request_stock_program_mode()
    batch.confirm_stock_program_mode()

    batch.record_stock_programming(1, succeeded=False)

    assert batch.state is BatchState.HOLD
    assert batch.slots[1].state is DeviceState.HOLD

    with pytest.raises(BatchOnHold):
        batch.request_stock_rf_mode()


@pytest.mark.unit
def test_golden_rig_is_used_only_for_stock_mac() -> None:
    batch = make_batch()
    advance_to_stock_rf(batch)

    for slot in batch.ordered_slots:
        batch.record_stock_rf_result(
            slot.number,
            str(slot.stock_mac),
        )

    assert batch.state is BatchState.STOCK_RF_CONFIRMED

    assert not hasattr(Batch, "begin_pricol_rf_test")
    assert not hasattr(Batch, "record_pricol_rf_result")
    assert not hasattr(Batch, "request_pricol_rf_mode")


@pytest.mark.unit
def test_wrong_stock_rf_identity_holds_batch() -> None:
    batch = make_batch()
    advance_to_stock_rf(batch)

    with pytest.raises(VerificationMismatch):
        batch.record_stock_rf_result(
            1,
            "AA:BB:CC:FF:FF:FF",
        )

    assert batch.state is BatchState.HOLD


@pytest.mark.unit
def test_pricol_macs_are_different_from_stock_macs() -> None:
    batch = make_batch()
    advance_to_stock_rf(batch)

    for slot in batch.ordered_slots:
        batch.record_stock_rf_result(
            slot.number,
            str(slot.stock_mac),
        )

    records = reserve_records(
        batch,
        purpose=MacPurpose.PRICOL_PRODUCTION,
        prefix="DD:EE:FF:00:00",
    )
    batch.accept_pricol_reservations(records)

    for slot in batch.ordered_slots:
        assert slot.stock_mac is not None
        assert slot.pricol_mac is not None
        assert slot.stock_mac != slot.pricol_mac


@pytest.mark.unit
def test_pricol_readback_leads_directly_to_functional_test() -> None:
    batch = make_batch()
    advance_to_pricol_readback(batch)

    for slot in batch.ordered_slots:
        batch.record_pricol_readback(
            slot.number,
            str(slot.pricol_mac),
        )

    assert batch.state is BatchState.PRICOL_READBACK_VERIFIED

    batch.request_functional_test_mode()

    assert batch.state is BatchState.AWAITING_FUNCTIONAL_TEST_MODE


@pytest.mark.unit
def test_wrong_pricol_readback_holds_batch() -> None:
    batch = make_batch()
    advance_to_pricol_readback(batch)

    with pytest.raises(VerificationMismatch):
        batch.record_pricol_readback(
            1,
            "DD:EE:FF:FF:FF:FF",
        )

    assert batch.state is BatchState.HOLD


@pytest.mark.unit
def test_dfu_requires_all_app_results_and_second_reset() -> None:
    batch = make_batch()
    advance_to_functional_app_test(batch)

    batch.record_pricol_app(1, True)

    with pytest.raises(InvalidBatchTransition):
        batch.confirm_pricol_dfu_reset()

    for slot_number in range(2, batch.slot_count + 1):
        batch.record_pricol_app(slot_number, True)

    assert batch.state is BatchState.AWAITING_PRICOLDFU_RESET

    batch.confirm_pricol_dfu_reset()

    assert batch.state is BatchState.PRICOLDFU_TESTING


@pytest.mark.unit
def test_functional_failure_can_be_committed() -> None:
    batch = make_batch()
    advance_to_functional_app_test(batch)

    for slot in batch.ordered_slots:
        batch.record_pricol_app(
            slot.number,
            passed=slot.number != 2,
        )

    batch.confirm_pricol_dfu_reset()

    for slot in batch.ordered_slots:
        batch.record_pricol_dfu(
            slot.number,
            passed=True,
        )

    assert batch.state is BatchState.FUNCTIONAL_TEST_COMPLETE
    assert batch.slots[2].state is DeviceState.FUNCTIONAL_TEST_FAILED

    # Post-test identity binding is still required.
    assert not batch.can_commit

    batch.begin_qr_scanning()

    for number in range(1, batch.slot_count + 1):
        bound = batch.scan_module_qr(f"MODULE-{number:03d}")
        assert bound == number

    assert batch.state is BatchState.QR_BOUND
    assert batch.slots[2].state is DeviceState.QR_BOUND
    assert batch.can_commit

    batch.commit()
    batch.queue_upload()
    batch.mark_uploaded()

    assert batch.state is BatchState.UPLOADED



def advance_to_qr_scanning(batch: Batch) -> None:
    advance_to_functional_app_test(batch)

    for slot in batch.ordered_slots:
        batch.record_pricol_app(
            slot.number,
            passed=True,
        )

    batch.confirm_pricol_dfu_reset()

    for slot in batch.ordered_slots:
        batch.record_pricol_dfu(
            slot.number,
            passed=True,
        )

    assert batch.state is BatchState.FUNCTIONAL_TEST_COMPLETE

    batch.begin_qr_scanning()

    assert batch.state is BatchState.QR_SCANNING
    assert batch.next_qr_slot == 1


@pytest.mark.unit
def test_qr_scans_bind_to_slots_in_strict_order() -> None:
    batch = make_batch()
    advance_to_qr_scanning(batch)

    assert batch.scan_module_qr("MODULE-001") == 1
    assert batch.next_qr_slot == 2

    assert batch.scan_module_qr("MODULE-002") == 2
    assert batch.next_qr_slot == 3

    assert batch.scan_module_qr("MODULE-003") == 3
    assert batch.next_qr_slot == 4

    assert batch.scan_module_qr("MODULE-004") == 4

    assert batch.next_qr_slot is None
    assert batch.state is BatchState.QR_BOUND


@pytest.mark.unit
def test_duplicate_qr_scan_is_rejected_without_advancing_slot() -> None:
    batch = make_batch()
    advance_to_qr_scanning(batch)

    batch.scan_module_qr("MODULE-001")

    assert batch.next_qr_slot == 2

    with pytest.raises(DuplicateModuleQr):
        batch.scan_module_qr("MODULE-001")

    assert batch.next_qr_slot == 2
    assert batch.slots[2].module_qr is None


@pytest.mark.unit
def test_batch_cannot_commit_before_all_qrs_are_scanned() -> None:
    batch = make_batch()
    advance_to_qr_scanning(batch)

    batch.scan_module_qr("MODULE-001")

    assert not batch.can_commit

    with pytest.raises(InvalidBatchTransition):
        batch.commit()


@pytest.mark.unit
def test_batch_commits_after_all_post_test_qrs_are_scanned() -> None:
    batch = make_batch()
    advance_to_qr_scanning(batch)

    for number in range(1, batch.slot_count + 1):
        bound_slot = batch.scan_module_qr(
            f"MODULE-{number:03d}"
        )
        assert bound_slot == number

    assert batch.state is BatchState.QR_BOUND
    assert batch.can_commit

    batch.commit()
    batch.queue_upload()

    assert batch.state is BatchState.UPLOAD_PENDING


@pytest.mark.unit
def test_duplicate_module_qr_is_rejected_during_post_test_scan() -> None:
    batch = make_batch()
    advance_to_qr_scanning(batch)

    batch.scan_module_qr("MODULE-001")

    with pytest.raises(DuplicateModuleQr):
        batch.scan_module_qr("MODULE-001")


@pytest.mark.unit
def test_complete_batch_can_be_committed_and_queued() -> None:
    batch = make_batch()
    advance_to_qr_scanning(batch)

    for number in range(1, batch.slot_count + 1):
        assert batch.scan_module_qr(f"MODULE-{number:03d}") == number

    assert batch.state is BatchState.QR_BOUND
    assert batch.can_commit

    batch.commit()
    batch.queue_upload()
    batch.mark_uploaded()

    assert batch.state is BatchState.UPLOADED

@pytest.mark.unit
def test_batch_rf_mode_requires_stock_readback() -> None:
    batch = make_batch()

    bind_batch_ports(batch)

    records = reserve_records(
        batch,
        purpose=MacPurpose.STOCK_RF_TEST,
        prefix="AA:BB:CC:00:00",
    )

    batch.accept_stock_reservations(records)
    batch.request_stock_program_mode()
    batch.confirm_stock_program_mode()

    for slot in batch.ordered_slots:
        batch.record_stock_programming(
            slot.number,
            succeeded=True,
        )

    assert batch.state is BatchState.STOCK_PROGRAMMED

    with pytest.raises(InvalidBatchTransition):
        batch.request_stock_rf_mode()

    for slot in batch.ordered_slots:
        assert slot.stock_mac is not None

        batch.record_stock_readback(
            slot.number,
            str(slot.stock_mac),
        )

    assert (
        batch.state
        is BatchState.STOCK_READBACK_VERIFIED
    )

    batch.request_stock_rf_mode()

    assert (
        batch.state
        is BatchState.AWAITING_STOCK_RF_MODE
    )


