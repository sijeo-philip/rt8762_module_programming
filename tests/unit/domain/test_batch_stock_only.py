"""Stock-only programming mode and final slot dispositions."""

import pytest

from stationapp.domain import (
    AllocatedMac,
    Batch,
    BatchState,
    DeviceState,
    MacAddress,
    MacPurpose,
    ProgrammingMode,
    SlotDisposition,
)
from stationapp.domain.errors import InvalidBatchTransition


def make_rf_confirmed_batch(mode: ProgrammingMode, occupied=(1, 3)) -> Batch:
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-4UP",
        slot_count=4,
        batch_id="BATCH-STOCK-ONLY",
        programming_mode=mode,
    )
    batch.configure_occupied_slots(tuple(occupied))
    batch.begin_port_binding()

    records = []
    for number in occupied:
        batch.bind_port(number, f"USB-{number}")
        record = AllocatedMac(
            allocation_id="A-1",
            address=MacAddress.parse(f"AA:BB:CC:00:00:{number:02X}"),
            purpose=MacPurpose.STOCK_RF_TEST,
        )
        record.reserve(batch_id=batch.batch_id, slot_number=number)
        records.append(record)

    batch.accept_stock_reservations(tuple(records))
    batch.request_stock_program_mode()
    batch.confirm_stock_program_mode()
    for number in occupied:
        batch.record_stock_programming(number, succeeded=True)
    for number in occupied:
        batch.record_stock_readback(number, str(batch.slots[number].stock_mac))

    # Fixture-only RF confirmation; production applies Golden Rig evidence.
    batch.request_stock_rf_mode()
    batch.begin_stock_rf_test()
    for number in occupied:
        batch.slots[number].verify_stock_rf(batch.slots[number].stock_mac)
    batch.state = BatchState.STOCK_RF_CONFIRMED
    return batch


@pytest.mark.unit
def test_default_mode_is_stock_and_pricol():
    batch = Batch(station_id="S", jig_id="J", slot_count=4)

    assert batch.programming_mode is ProgrammingMode.STOCK_AND_PRICOL
    assert batch.requires_pricol


@pytest.mark.unit
def test_stock_only_goes_from_rf_to_qr_scanning():
    batch = make_rf_confirmed_batch(ProgrammingMode.STOCK_ONLY)

    batch.begin_qr_scanning()

    assert batch.state is BatchState.QR_SCANNING
    assert [slot.number for slot in batch.qr_candidates] == [1, 3]


@pytest.mark.unit
def test_stock_only_never_reserves_pricol_macs():
    batch = make_rf_confirmed_batch(ProgrammingMode.STOCK_ONLY)

    with pytest.raises(InvalidBatchTransition, match="Stock-only"):
        batch.accept_pricol_reservations(())


@pytest.mark.unit
def test_full_mode_cannot_skip_to_qr_after_rf():
    batch = make_rf_confirmed_batch(ProgrammingMode.STOCK_AND_PRICOL)

    with pytest.raises(InvalidBatchTransition):
        batch.begin_qr_scanning()


@pytest.mark.unit
def test_stock_only_qr_binding_commits_with_pass_dispositions():
    batch = make_rf_confirmed_batch(ProgrammingMode.STOCK_ONLY)
    batch.begin_qr_scanning()

    assert batch.scan_module_qr("QR-1") == 1
    assert batch.scan_module_qr("QR-3") == 3
    assert batch.state is BatchState.QR_BOUND
    assert batch.slots[1].stock_mac_record.module_qr == "QR-1"
    assert batch.can_commit

    batch.commit()

    assert batch.dispositions == {
        1: SlotDisposition.PASS,
        2: SlotDisposition.EMPTY,
        3: SlotDisposition.PASS,
        4: SlotDisposition.EMPTY,
    }


@pytest.mark.unit
def test_held_slot_is_hold_disposition_not_pass():
    batch = make_rf_confirmed_batch(ProgrammingMode.STOCK_ONLY, occupied=(1, 2))
    batch.slots[2].state = DeviceState.STOCK_READBACK_VERIFIED
    batch.slots[2].place_on_hold("RF FAIL")

    batch.begin_qr_scanning()
    batch.scan_module_qr("QR-1")

    assert batch.state is BatchState.QR_BOUND
    assert batch.disposition_for(1) is SlotDisposition.PASS
    assert batch.disposition_for(2) is SlotDisposition.HOLD
    assert batch.can_commit
