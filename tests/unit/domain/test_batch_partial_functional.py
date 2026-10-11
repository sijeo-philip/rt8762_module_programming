
import pytest

from stationapp.domain import (
    AllocatedMac,
    Batch,
    BatchState,
    DeviceState,
    MacAddress,
    MacPurpose,
)

from stationapp.domain.errors import (
    InvalidBatchTransition,
)


def make_functional_batch(
    *,
    candidates=(1, 4),
    occupied=(1, 2, 4, 7),
) -> Batch:
    """Create a state-specific fixture for functional lifecycle tests.

    Pricol and Stock MAC records are confirmed using real
    domain transitions. Earlier RF evidence is covered by
    the 9F and 9G.2 tests.
    """
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-8UP",
        slot_count=8,
        batch_id="BATCH-9G4-001",
    )

    batch.configure_occupied_slots(tuple(occupied))

    for number in occupied:
        slot = batch.slots[number]
        slot.bind_port(f"USB-SERIAL-{number:03d}")

        stock = AllocatedMac(
            allocation_id=f"STOCK-{number}",
            address=MacAddress.parse(
                f"AA:BB:CC:00:00:{number:02X}"
            ),
            purpose=MacPurpose.STOCK_RF_TEST,
        )
        stock.reserve(
            batch_id=batch.batch_id,
            slot_number=number,
        )

        slot.assign_stock_mac(stock)
        slot.begin_stock_programming()
        slot.complete_stock_programming(True)
        slot.verify_stock_readback(stock.address)

        if number not in candidates:
            slot.place_on_hold(
                "Golden Rig RF verification failed"
            )
            continue

        # Fixture-only identity confirmation.
        # The real production path uses validated
        # GoldenRigResultProcessor evidence.
        slot.verify_stock_rf(stock.address)

        pricol = AllocatedMac(
            allocation_id=f"PRICOL-{number}",
            address=MacAddress.parse(
                f"DD:EE:FF:00:00:{number:02X}"
            ),
            purpose=MacPurpose.PRICOL_PRODUCTION,
        )
        pricol.reserve(
            batch_id=batch.batch_id,
            slot_number=number,
        )

        slot.assign_pricol_mac(pricol)
        slot.begin_pricol_programming()
        slot.complete_pricol_programming(True)
        slot.verify_pricol_readback(pricol.address)

    batch.state = BatchState.PRICOL_READBACK_VERIFIED

    batch.request_functional_test_mode()
    batch.confirm_functional_test_mode()
    batch.confirm_pricol_app_reset()

    return batch


@pytest.mark.unit
def test_only_pricol_verified_slots_receive_functional_tests():
    batch = make_functional_batch()

    assert batch.functional_app_targets == (1, 4)

    with pytest.raises(InvalidBatchTransition):
        batch.record_pricol_app(2, True)

    with pytest.raises(InvalidBatchTransition):
        batch.record_pricol_app(3, True)

    assert batch.state is BatchState.PRICOLAPP_TESTING


@pytest.mark.unit
def test_app_completion_waits_only_for_candidates():
    batch = make_functional_batch()

    batch.record_pricol_app(1, True)

    assert batch.state is BatchState.PRICOLAPP_TESTING

    batch.record_pricol_app(4, False)

    assert batch.state is BatchState.AWAITING_PRICOLDFU_RESET
    assert batch.functional_dfu_targets == (1, 4)


@pytest.mark.unit
def test_dfu_requires_second_reset():
    batch = make_functional_batch()

    batch.record_pricol_app(1, True)
    batch.record_pricol_app(4, True)

    assert batch.state is BatchState.AWAITING_PRICOLDFU_RESET

    with pytest.raises(InvalidBatchTransition):
        batch.record_pricol_dfu(1, True)

    batch.confirm_pricol_dfu_reset()

    assert batch.state is BatchState.PRICOLDFU_TESTING


@pytest.mark.unit
def test_functional_failure_still_requires_qr():
    batch = make_functional_batch()

    batch.record_pricol_app(1, True)
    batch.record_pricol_app(4, False)

    batch.confirm_pricol_dfu_reset()

    batch.record_pricol_dfu(1, True)
    batch.record_pricol_dfu(4, True)

    assert batch.state is BatchState.FUNCTIONAL_TEST_COMPLETE
    assert batch.slots[1].state is DeviceState.FUNCTIONAL_TEST_PASSED
    assert batch.slots[4].state is DeviceState.FUNCTIONAL_TEST_FAILED

    batch.begin_qr_scanning()

    assert batch.next_qr_slot == 1
    assert batch.scan_module_qr("MODULE-001") == 1

    assert batch.next_qr_slot == 4
    assert batch.scan_module_qr("MODULE-004") == 4

    assert batch.next_qr_slot is None
    assert batch.state is BatchState.QR_BOUND

    assert batch.releasable_slot_numbers == (1,)
    assert 4 in batch.rejected_slot_numbers


@pytest.mark.unit
def test_held_duts_do_not_block_mixed_batch_commit():
    batch = make_functional_batch()

    batch.record_pricol_app(1, True)
    batch.record_pricol_app(4, True)

    batch.confirm_pricol_dfu_reset()

    batch.record_pricol_dfu(1, True)
    batch.record_pricol_dfu(4, False)

    batch.begin_qr_scanning()
    batch.scan_module_qr("MODULE-001")
    batch.scan_module_qr("MODULE-004")

    assert batch.can_commit
    assert batch.releasable_slot_numbers == (1,)
    assert set(batch.rejected_slot_numbers) == {2, 4, 7}

    batch.commit()

    assert batch.state is BatchState.COMMITTED


@pytest.mark.unit
def test_app_timeout_quarantines_only_one_slot():
    batch = make_functional_batch()

    batch.record_pricol_app_unverified(
        1,
        reason="AT command timeout",
    )

    assert batch.slots[1].state is DeviceState.HOLD
    assert batch.state is BatchState.PRICOLAPP_TESTING

    batch.record_pricol_app(4, True)

    assert batch.state is BatchState.AWAITING_PRICOLDFU_RESET

    batch.confirm_pricol_dfu_reset()
    batch.record_pricol_dfu(4, True)

    assert batch.state is BatchState.FUNCTIONAL_TEST_COMPLETE

    batch.begin_qr_scanning()
    assert batch.next_qr_slot == 4


@pytest.mark.unit
def test_dfu_timeout_quarantines_only_one_slot():
    batch = make_functional_batch()

    batch.record_pricol_app(1, True)
    batch.record_pricol_app(4, True)

    batch.confirm_pricol_dfu_reset()

    batch.record_pricol_dfu_unverified(
        1,
        reason="DFU communication lost",
    )

    assert batch.slots[1].state is DeviceState.HOLD
    assert batch.state is BatchState.PRICOLDFU_TESTING

    batch.record_pricol_dfu(4, True)

    assert batch.state is BatchState.FUNCTIONAL_TEST_COMPLETE


@pytest.mark.unit
def test_duplicate_app_result_is_rejected():
    batch = make_functional_batch()

    batch.record_pricol_app(1, True)

    with pytest.raises(InvalidBatchTransition):
        batch.record_pricol_app(1, False)

    assert batch.slots[1].functional.pricol_app_passed is True


@pytest.mark.unit
def test_no_remaining_functional_dut_holds_batch():
    batch = make_functional_batch()

    batch.record_pricol_app_unverified(
        1, reason="No response"
    )
    batch.record_pricol_app_unverified(
        4, reason="No response"
    )

    assert batch.state is BatchState.HOLD
    assert batch.slots[1].state is DeviceState.HOLD
    assert batch.slots[4].state is DeviceState.HOLD
