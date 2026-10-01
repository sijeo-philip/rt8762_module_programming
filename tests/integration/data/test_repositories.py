"""Integration tests for Lesson 5 transactional repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from stationapp.data.base import Base
from stationapp.data.database import (
    create_session_factory,
    create_station_engine,
)
from stationapp.data.errors import (
    AllocationImportConflict,
    DuplicateMacInDatabase,
    DuplicateModuleQrInDatabase,
    IncompleteSlotIdentity,
    InsufficientMacAvailability,
    NoQrSlotAvailable,
)
from stationapp.data.models import (
    AllocatedMacModel,
    BatchModel,
    BatchSlotModel,
)
from stationapp.data.repositories import (
    AllocationRepository,
    BatchRepository,
    RecoveryRepository,
    utc_now,
)
from stationapp.domain.allocation import AllocationDocument
from stationapp.domain.batch import Batch
from stationapp.domain.mac import (
    MacAddress,
    MacPurpose,
    MacStatus,
)

@pytest.fixture
def persistence(tmp_path: Path):
    database_file = tmp_path / "repository.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    Base.metadata.create_all(engine)

    session_factory = create_session_factory(engine)

    try:
        yield (
            engine,
            session_factory,
            AllocationRepository(session_factory),
            BatchRepository(session_factory),
            RecoveryRepository(session_factory),
        )
    finally:
        engine.dispose()

def make_allocation(*, allocation_id: str, purpose: MacPurpose, start_value: int, count: int) -> AllocationDocument:
    addresses = tuple(
        MacAddress(start_value + offset)
        for offset in range(count)
    )

    return AllocationDocument(
        allocation_id=allocation_id,
        station_id="STATION-01",
        jig_id="JIG-01",
        purpose=purpose,
        issued_at=datetime.now(timezone.utc),
        addresses=addresses,
    )


@pytest.mark.integration
def test_allocation_import_persists_every_mac(persistence) -> None:
    _, session_factory, allocations, _, _ = persistence

    document = make_allocation(
        allocation_id="ALLOC-STOCK-001",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=8,
    )

    imported = allocations.import_allocation(
        document,
        document_digest="digest-stock-001",
    )

    assert imported is True

    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AllocatedMacModel)
        )

        assert count == 8

        statuses = set(
            session.scalars(
                select(AllocatedMacModel.status)
            ).all()
        )

        assert statuses == {
            MacStatus.AVAILABLE.value
        }


@pytest.mark.integration
def test_same_allocation_document_is_idempotent(persistence) -> None:
    _, session_factory, allocations, _, _ = persistence

    document = make_allocation(
        allocation_id="ALLOC-STOCK-001",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    assert allocations.import_allocation(
        document,
        document_digest="same-digest",
    ) is True

    assert allocations.import_allocation(
        document,
        document_digest="same-digest",
    ) is False

    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AllocatedMacModel)
        )

        assert count == 4

@pytest.mark.integration
def test_changed_document_with_same_allocation_id_is_rejected(
    persistence,
) -> None:
    _, _, allocations, _, _ = persistence

    document = make_allocation(
        allocation_id="ALLOC-STOCK-001",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    allocations.import_allocation(
        document,
        document_digest="digest-version-one",
    )

    with pytest.raises(AllocationImportConflict):
        allocations.import_allocation(
            document,
            document_digest="digest-version-two",
        )

@pytest.mark.integration
def test_mac_cannot_be_imported_from_second_allocation(persistence) -> None:
    _, session_factory, allocations, _, _ = persistence

    first = make_allocation(
        allocation_id="ALLOC-001",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    second = make_allocation(
        allocation_id="ALLOC-002",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE02,
        count=4,
    )

    allocations.import_allocation(
        first,
        document_digest="digest-one",
    )

    with pytest.raises(DuplicateMacInDatabase):
        allocations.import_allocation(
            second,
            document_digest="digest-two",
        )

    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AllocatedMacModel)
        )

        assert count == 4

@pytest.mark.integration
@pytest.mark.parametrize(
    "slot_count",
    [4, 8],
)
def test_create_batch_persists_exact_slot_count(
    persistence,
    slot_count: int,
) -> None:
    _, session_factory, _, batches, _ = persistence

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=slot_count,
    )

    batches.create_batch(batch)

    with session_factory() as session:
        stored_batch = session.get(
            BatchModel,
            batch.batch_id,
        )

        assert stored_batch is not None
        assert stored_batch.slot_count == slot_count

        slots = session.scalars(
            select(BatchSlotModel.slot_number)
            .where(
                BatchSlotModel.batch_id
                == batch.batch_id
            )
            .order_by(
                BatchSlotModel.slot_number
            )
        ).all()

        assert slots == list(
            range(1, slot_count + 1)
        )

@pytest.mark.integration
def test_atomic_reservation_assigns_one_mac_per_slot(persistence) -> None:
    _, session_factory, allocations, batches,_ = persistence

    document = make_allocation(
        allocation_id="ALLOC-STOCK",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=8,
    )

    allocations.import_allocation(
        document,
        document_digest="stock-digest",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    reserved = batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    assert len(reserved) == 4

    assert [
        record.slot_number
        for record in reserved
    ] == [1, 2, 3, 4]

    assert all(
        record.status is MacStatus.RESERVED
        for record in reserved
    )

    with session_factory() as session:
        persisted = session.scalars(
            select(AllocatedMacModel)
            .where(
                AllocatedMacModel.batch_id
                == batch.batch_id
            )
            .order_by(
                AllocatedMacModel.slot_number
            )
        ).all()

        assert len(persisted) == 4

        assert [
            item.slot_number
            for item in persisted
        ] == [1, 2, 3, 4]

@pytest.mark.integration
def test_insufficient_availability_reserves_nothing(persistence) -> None:
    _, session_factory, allocations, batches,_ = persistence

    document = make_allocation(
        allocation_id="ALLOC-SHORT",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=3,
    )

    allocations.import_allocation(
        document,
        document_digest="short-digest",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    with pytest.raises(InsufficientMacAvailability):
        batches.reserve_macs(
            batch_id=batch.batch_id,
            purpose=MacPurpose.STOCK_RF_TEST,
        )

    with session_factory() as session:
        records = session.scalars(
            select(AllocatedMacModel)
        ).all()

        assert len(records) == 3

        assert all(
            record.status == MacStatus.AVAILABLE.value
            for record in records
        )

        assert all(
            record.batch_id is None
            for record in records
        )

        assert all(
            record.slot_number is None
            for record in records
        )

@pytest.mark.integration
def test_reservation_uses_only_requested_purpose(
    persistence,
) -> None:
    _, session_factory, allocations, batches, _ = persistence

    stock = make_allocation(
        allocation_id="ALLOC-STOCK",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    pricol = make_allocation(
        allocation_id="ALLOC-PRICOL",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        start_value=0xAABBCCDDEF00,
        count=4,
    )

    allocations.import_allocation(
        stock,
        document_digest="stock",
    )

    allocations.import_allocation(
        pricol,
        document_digest="pricol",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    with session_factory() as session:
        pricol_records = session.scalars(
            select(AllocatedMacModel)
            .where(
                AllocatedMacModel.purpose
                == MacPurpose.PRICOL_PRODUCTION.value
            )
        ).all()

        assert len(pricol_records) == 4

        assert all(
            record.status
            == MacStatus.AVAILABLE.value
            for record in pricol_records
        )


def prepare_dual_mac_batch(
    persistence,
    *,
    slot_count: int = 4,
):
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence

    stock = make_allocation(
        allocation_id="ALLOC-STOCK-QR",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=slot_count,
    )

    pricol = make_allocation(
        allocation_id="ALLOC-PRICOL-QR",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        start_value=0xAABBCCDDEF00,
        count=slot_count,
    )

    allocations.import_allocation(
        stock,
        document_digest="digest-stock-qr",
    )

    allocations.import_allocation(
        pricol,
        document_digest="digest-pricol-qr",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=slot_count,
    )

    batches.create_batch(batch)

    batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    return (
        session_factory,
        batches,
        batch,
    )

@pytest.mark.integration
def test_first_qr_binds_slot_one(
    persistence,
) -> None:
    session_factory, batches, batch = (
        prepare_dual_mac_batch(persistence)
    )

    slot_number = batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0001",
    )

    assert slot_number == 1

    with session_factory() as session:
        slot = session.scalar(
            select(BatchSlotModel)
            .where(
                BatchSlotModel.batch_id == batch.batch_id,
                BatchSlotModel.slot_number == 1,
            )
        )

        assert slot is not None
        assert slot.module_qr == "MODULE-0001"

@pytest.mark.integration
def test_qr_propagates_to_both_mac_records(
    persistence,
) -> None:
    session_factory, batches, batch = (
        prepare_dual_mac_batch(persistence)
    )

    batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0001",
    )

    with session_factory() as session:
        records = session.scalars(
            select(AllocatedMacModel)
            .where(
                AllocatedMacModel.batch_id == batch.batch_id,
                AllocatedMacModel.slot_number == 1,
            )
            .order_by(
                AllocatedMacModel.purpose
            )
        ).all()

        assert len(records) == 2

        assert {
            record.purpose
            for record in records
        } == {
            MacPurpose.STOCK_RF_TEST.value,
            MacPurpose.PRICOL_PRODUCTION.value,
        }

        assert all(
            record.module_qr == "MODULE-0001"
            for record in records
        )


@pytest.mark.integration
def test_qr_scanning_advances_in_strict_slot_order(
    persistence,
) -> None:
    _, batches, batch = prepare_dual_mac_batch(
        persistence
    )

    assert batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0001",
    ) == 1

    assert batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0002",
    ) == 2

    assert batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0003",
    ) == 3

    assert batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0004",
    ) == 4

@pytest.mark.integration
def test_duplicate_qr_does_not_advance_slot(
    persistence,
) -> None:
    session_factory, batches, batch = (
        prepare_dual_mac_batch(persistence)
    )

    batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0001",
    )

    with pytest.raises(
        DuplicateModuleQrInDatabase
    ):
        batches.bind_next_module_qr(
            batch_id=batch.batch_id,
            module_qr="MODULE-0001",
        )

    # The next valid QR must still bind to Slot 2.
    slot_number = batches.bind_next_module_qr(
        batch_id=batch.batch_id,
        module_qr="MODULE-0002",
    )

    assert slot_number == 2

    with session_factory() as session:
        slot_two = session.scalar(
            select(BatchSlotModel)
            .where(
                BatchSlotModel.batch_id == batch.batch_id,
                BatchSlotModel.slot_number == 2,
            )
        )

        assert slot_two is not None
        assert slot_two.module_qr == "MODULE-0002"


@pytest.mark.integration
def test_qr_binding_rolls_back_if_second_mac_is_missing(
    persistence,
) -> None:
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence

    stock = make_allocation(
        allocation_id="ALLOC-STOCK-ONLY",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    allocations.import_allocation(
        stock,
        document_digest="stock-only",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    with pytest.raises(IncompleteSlotIdentity):
        batches.bind_next_module_qr(
            batch_id=batch.batch_id,
            module_qr="MODULE-0001",
        )

    with session_factory() as session:
        slot = session.scalar(
            select(BatchSlotModel)
            .where(
                BatchSlotModel.batch_id == batch.batch_id,
                BatchSlotModel.slot_number == 1,
            )
        )

        assert slot is not None
        assert slot.module_qr is None

        stock_record = session.scalar(
            select(AllocatedMacModel)
            .where(
                AllocatedMacModel.batch_id == batch.batch_id,
                AllocatedMacModel.slot_number == 1,
            )
        )

        assert stock_record is not None
        assert stock_record.module_qr is None

@pytest.mark.integration
def test_scanning_after_last_slot_is_rejected(
    persistence,
) -> None:
    _, batches, batch = prepare_dual_mac_batch(
        persistence
    )

    for number in range(1, 5):
        batches.bind_next_module_qr(
            batch_id=batch.batch_id,
            module_qr=f"MODULE-{number:04d}",
        )

    with pytest.raises(NoQrSlotAvailable):
        batches.bind_next_module_qr(
            batch_id=batch.batch_id,
            module_qr="MODULE-9999",
        )
@pytest.mark.integration
def test_startup_recovery_moves_programming_mac_to_hold(
    persistence,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        recovery,
    ) = persistence

    document = make_allocation(
        allocation_id="ALLOC-RECOVERY",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=1,
    )

    allocations.import_allocation(
        document,
        document_digest="recovery-digest",
    )

    with session_factory() as session:
        record = session.scalar(
            select(AllocatedMacModel)
        )

        assert record is not None

        record.status = MacStatus.PROGRAMMING.value
        record.programming_started_at = utc_now()

        session.commit()

    recovered = recovery.recover_uncertain_programming()

    assert recovered == 1

    with session_factory() as session:
        record = session.scalar(
            select(AllocatedMacModel)
        )

        assert record is not None
        assert record.status == MacStatus.HOLD.value
        assert record.held_at is not None

        assert "uncertain" in record.hold_reason.lower()

@pytest.mark.integration
def test_startup_recovery_does_not_touch_available_mac(
    persistence,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        recovery,
    ) = persistence

    document = make_allocation(
        allocation_id="ALLOC-AVAILABLE",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=2,
    )

    allocations.import_allocation(
        document,
        document_digest="available-digest",
    )

    recovered = recovery.recover_uncertain_programming()

    assert recovered == 0

    with session_factory() as session:
        records = session.scalars(
            select(AllocatedMacModel)
        ).all()

        assert all(
            record.status == MacStatus.AVAILABLE.value
            for record in records
        )

@pytest.mark.integration
def test_recovery_quarantines_only_programming_records(
    persistence,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        recovery,
    ) = persistence

    document = make_allocation(
        allocation_id="ALLOC-MIXED",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    allocations.import_allocation(
        document,
        document_digest="mixed-digest",
    )

    with session_factory() as session:
        records = session.scalars(
            select(AllocatedMacModel)
            .order_by(
                AllocatedMacModel.id
            )
        ).all()

        records[0].status = MacStatus.AVAILABLE.value
        records[1].status = MacStatus.RESERVED.value
        records[2].status = MacStatus.PROGRAMMING.value
        records[3].status = MacStatus.CONFIRMED.value

        session.commit()

    recovered = recovery.recover_uncertain_programming()

    assert recovered == 1

    with session_factory() as session:
        records = session.scalars(
            select(AllocatedMacModel)
            .order_by(
                AllocatedMacModel.id
            )
        ).all()

        assert records[0].status == MacStatus.AVAILABLE.value
        assert records[1].status == MacStatus.RESERVED.value
        assert records[2].status == MacStatus.HOLD.value
        assert records[3].status == MacStatus.CONFIRMED.value


@pytest.mark.integration
def test_startup_recovery_is_idempotent(
    persistence,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        recovery,
    ) = persistence

    document = make_allocation(
        allocation_id="ALLOC-IDEMPOTENT",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=1,
    )

    allocations.import_allocation(
        document,
        document_digest="idempotent-recovery",
    )

    with session_factory() as session:
        record = session.scalar(
            select(AllocatedMacModel)
        )

        record.status = MacStatus.PROGRAMMING.value
        record.programming_started_at = utc_now()

        session.commit()

    first = recovery.recover_uncertain_programming()
    second = recovery.recover_uncertain_programming()

    assert first == 1
    assert second == 0

