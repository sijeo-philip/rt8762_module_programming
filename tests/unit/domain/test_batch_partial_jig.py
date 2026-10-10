
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
from stationapp.domain.errors import InvalidBatchTransition


def make_partial_batch() -> Batch:
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-8UP",
        slot_count=8,
        batch_id="BATCH-PARTIAL-001",
    )
    batch.configure_occupied_slots((1, 2, 4, 7))
    return batch


def prepare_stock_programming() -> Batch:
    batch = make_partial_batch()
    batch.begin_port_binding()

    for n in (1, 2, 4, 7):
        batch.bind_port(n, f"USB-SERIAL-{n:03d}")

    assert batch.state is BatchState.PORTS_BOUND

    records = []

    for n in (1, 2, 4, 7):
        record = AllocatedMac(
            allocation_id=f"STOCK-{n}",
            address=MacAddress.parse(
                f"AA:BB:CC:00:00:{n:02X}"
            ),
            purpose=MacPurpose.STOCK_RF_TEST,
        )
        record.reserve(
            batch_id=batch.batch_id,
            slot_number=n,
        )
        records.append(record)

    batch.accept_stock_reservations(tuple(records))
    batch.request_stock_program_mode()
    batch.confirm_stock_program_mode()

    return batch


@pytest.mark.unit
def test_partial_jig_occupancy() -> None:
    batch = make_partial_batch()

    assert batch.occupied_count == 4
    assert batch.occupied_slot_numbers == (1, 2, 4, 7)
    assert batch.empty_slot_numbers == (3, 5, 6, 8)


@pytest.mark.unit
def test_reject_invalid_occupancy() -> None:
    batch = make_partial_batch()

    with pytest.raises(ValueError):
        batch.configure_occupied_slots((1, 1, 4))

    with pytest.raises(ValueError):
        batch.configure_occupied_slots((1, 9))


@pytest.mark.unit
def test_occupancy_locked_after_start() -> None:
    batch = make_partial_batch()
    batch.begin_port_binding()

    with pytest.raises(InvalidBatchTransition):
        batch.configure_occupied_slots((1, 2))


@pytest.mark.unit
def test_partial_stock_programming() -> None:
    batch = prepare_stock_programming()

    batch.record_stock_programming(1, succeeded=True)
    batch.record_stock_programming(2, succeeded=False)
    batch.record_stock_programming(4, succeeded=True)
    batch.record_stock_programming(7, succeeded=True)

    assert batch.state is BatchState.STOCK_PROGRAMMED
    assert batch.slots[2].state is DeviceState.HOLD

    assert batch.stock_readback_targets == (1, 4, 7)

    for n in (3, 5, 6, 8):
        assert batch.slots[n].state is DeviceState.EMPTY


@pytest.mark.unit
def test_readback_failure_isolated_to_one_slot() -> None:
    batch = prepare_stock_programming()

    for n in (1, 2, 4, 7):
        batch.record_stock_programming(n, succeeded=True)

    batch.record_stock_readback(
        1, str(batch.slots[1].stock_mac)
    )

    with pytest.raises(VerificationMismatch):
        batch.record_stock_readback(
            2, "AA:BB:CC:00:00:99"
        )

    batch.record_stock_readback(
        4, str(batch.slots[4].stock_mac)
    )

    batch.record_stock_readback_unverified(
        7, reason="MPCLI timeout"
    )

    assert (
        batch.state
        is BatchState.STOCK_READBACK_VERIFIED
    )

    assert batch.slots[1].state is DeviceState.STOCK_READBACK_VERIFIED
    assert batch.slots[4].state is DeviceState.STOCK_READBACK_VERIFIED
    assert batch.slots[2].state is DeviceState.HOLD
    assert batch.slots[7].state is DeviceState.HOLD

    assert batch.slots[2].stock_mac_record.status is MacStatus.HOLD
    assert batch.slots[7].stock_mac_record.status is MacStatus.HOLD


@pytest.mark.unit
def test_all_stock_programming_failed() -> None:
    batch = prepare_stock_programming()

    for n in (1, 2, 4, 7):
        batch.record_stock_programming(n, succeeded=False)

    assert batch.state is BatchState.HOLD
    assert all(
        batch.slots[n].state is DeviceState.HOLD
        for n in (1, 2, 4, 7)
    )


@pytest.mark.unit
def test_unoccupied_slot_cannot_receive_result() -> None:
    batch = prepare_stock_programming()

    with pytest.raises(InvalidBatchTransition):
        batch.record_stock_programming(3, succeeded=True)

@pytest.mark.unit
def test_ordered_slots_preserved_for_partial_jig() -> None:
    batch = make_partial_batch()

    assert [
        slot.number
        for slot in batch.ordered_slots
    ] == [1, 2, 3, 4, 5, 6, 7, 8]

    assert [
        slot.number
        for slot in batch.occupied_slots
    ] == [1, 2, 4, 7]

