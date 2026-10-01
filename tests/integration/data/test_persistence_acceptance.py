"""Lesson 5F persistence acceptance and concurrency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    DuplicateMacInDatabase,
    InsufficientMacAvailability,
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
)
from stationapp.domain.allocation import AllocationDocument
from stationapp.domain.batch import Batch
from stationapp.domain.mac import (
    MacAddress,
    MacPurpose,
    MacStatus,
)

@pytest.fixture
def persistence_stack(tmp_path: Path):
    database_file = tmp_path / "acceptance.db"

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


def make_allocation(
    *,
    allocation_id: str,
    purpose: MacPurpose,
    start_value: int,
    count: int,
) -> AllocationDocument:
    return AllocationDocument(
        allocation_id=allocation_id,
        station_id="STATION-01",
        jig_id="JIG-01",
        purpose=purpose,
        issued_at=datetime.now(timezone.utc),
        addresses=tuple(
            MacAddress(start_value + offset)
            for offset in range(count)
        ),
    )


@pytest.mark.integration
def test_complete_four_slot_persistence_flow(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence_stack

    stock = make_allocation(
        allocation_id="STOCK-4",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    pricol = make_allocation(
        allocation_id="PRICOL-4",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        start_value=0xAABBCCDDEF00,
        count=4,
    )

    allocations.import_allocation(
        stock,
        document_digest="stock-4-digest",
    )

    allocations.import_allocation(
        pricol,
        document_digest="pricol-4-digest",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    stock_records = batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    pricol_records = batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    assert len(stock_records) == 4
    assert len(pricol_records) == 4

    for number in range(1, 5):
        slot = batches.bind_next_module_qr(
            batch_id=batch.batch_id,
            module_qr=f"MODULE-{number:04d}",
        )

        assert slot == number

    with session_factory() as session:
        persisted_batch = session.get(
            BatchModel,
            batch.batch_id,
        )

        assert persisted_batch is not None
        assert persisted_batch.slot_count == 4

        slots = session.scalars(
            select(BatchSlotModel)
            .where(
                BatchSlotModel.batch_id == batch.batch_id
            )
            .order_by(BatchSlotModel.slot_number)
        ).all()

        assert len(slots) == 4

        assert [
            slot.module_qr
            for slot in slots
        ] == [
            "MODULE-0001",
            "MODULE-0002",
            "MODULE-0003",
            "MODULE-0004",
        ]

        macs = session.scalars(
            select(AllocatedMacModel)
            .where(
                AllocatedMacModel.batch_id == batch.batch_id
            )
        ).all()

        assert len(macs) == 8

        assert all(
            mac.module_qr is not None
            for mac in macs
        )

@pytest.mark.integration
def test_complete_eight_slot_persistence_flow(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence_stack

    stock = make_allocation(
        allocation_id="STOCK-8",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDE000,
        count=8,
    )

    pricol = make_allocation(
        allocation_id="PRICOL-8",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        start_value=0xAABBCCDDF000,
        count=8,
    )

    allocations.import_allocation(
        stock,
        document_digest="stock-8-digest",
    )

    allocations.import_allocation(
        pricol,
        document_digest="pricol-8-digest",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=8,
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

    for number in range(1, 9):
        slot = batches.bind_next_module_qr(
            batch_id=batch.batch_id,
            module_qr=f"MODULE-{number:04d}",
        )

        assert slot == number

    with session_factory() as session:
        slot_count = session.scalar(
            select(func.count())
            .select_from(BatchSlotModel)
            .where(
                BatchSlotModel.batch_id == batch.batch_id
            )
        )

        mac_count = session.scalar(
            select(func.count())
            .select_from(AllocatedMacModel)
            .where(
                AllocatedMacModel.batch_id == batch.batch_id
            )
        )

        assert slot_count == 8
        assert mac_count == 16

@pytest.mark.integration
def test_failed_reservation_rolls_back_all_mac_changes(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence_stack

    document = make_allocation(
        allocation_id="SHORT-STOCK",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=3,
    )

    allocations.import_allocation(
        document,
        document_digest="short-stock",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    with pytest.raises(
        InsufficientMacAvailability
    ):
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

@pytest.mark.integration
def test_duplicate_mac_across_two_allocations_is_rejected(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        _,
    ) = persistence_stack

    first = make_allocation(
        allocation_id="ALLOC-A",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    second = make_allocation(
        allocation_id="ALLOC-B",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        start_value=0xAABBCCDDEE02,
        count=4,
    )

    allocations.import_allocation(
        first,
        document_digest="alloc-a",
    )

    with pytest.raises(DuplicateMacInDatabase):
        allocations.import_allocation(
            second,
            document_digest="alloc-b",
        )

    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AllocatedMacModel)
        )

        assert count == 4

@pytest.mark.integration
def test_two_batches_cannot_reserve_same_mac_pool(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence_stack

    document = make_allocation(
        allocation_id="CONCURRENT-STOCK",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDE000,
        count=8,
    )

    allocations.import_allocation(
        document,
        document_digest="concurrent-stock",
    )

    batch_a = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=8,
    )

    batch_b = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=8,
    )

    batches.create_batch(batch_a)
    batches.create_batch(batch_b)

    def reserve(batch_id: str):
        try:
            records = batches.reserve_macs(
                batch_id=batch_id,
                purpose=MacPurpose.STOCK_RF_TEST,
            )

            return (
                "success",
                batch_id,
                len(records),
            )

        except InsufficientMacAvailability:
            return (
                "insufficient",
                batch_id,
                0,
            )

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:
        futures = [
            executor.submit(
                reserve,
                batch_a.batch_id,
            ),
            executor.submit(
                reserve,
                batch_b.batch_id,
            ),
        ]

        results = [
            future.result()
            for future in futures
        ]

    successful = [
        result
        for result in results
        if result[0] == "success"
    ]

    insufficient = [
        result
        for result in results
        if result[0] == "insufficient"
    ]

    assert len(successful) == 1
    assert len(insufficient) == 1

    assert successful[0][2] == 8

    with session_factory() as session:
        reserved = session.scalars(
            select(AllocatedMacModel)
            .where(
                AllocatedMacModel.status
                == MacStatus.RESERVED.value
            )
        ).all()

        assert len(reserved) == 8

        winning_batch_ids = {
            record.batch_id
            for record in reserved
        }

        assert len(winning_batch_ids) == 1

@pytest.mark.integration
def test_stock_and_pricol_reservations_are_independent(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        batches,
        _,
    ) = persistence_stack

    stock = make_allocation(
        allocation_id="STOCK-INDEPENDENT",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=4,
    )

    pricol = make_allocation(
        allocation_id="PRICOL-INDEPENDENT",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        start_value=0xAABBCCDDEF00,
        count=4,
    )

    allocations.import_allocation(
        stock,
        document_digest="stock-independent",
    )

    allocations.import_allocation(
        pricol,
        document_digest="pricol-independent",
    )

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=4,
    )

    batches.create_batch(batch)

    stock_reserved = batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    pricol_reserved = batches.reserve_macs(
        batch_id=batch.batch_id,
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    assert len(stock_reserved) == 4
    assert len(pricol_reserved) == 4

    stock_addresses = {
        record.address
        for record in stock_reserved
    }

    pricol_addresses = {
        record.address
        for record in pricol_reserved
    }

    assert stock_addresses.isdisjoint(
        pricol_addresses
    )

@pytest.mark.integration
def test_restart_never_reuses_programming_mac(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        recovery,
    ) = persistence_stack

    document = make_allocation(
        allocation_id="RECOVERY-POOL",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=2,
    )

    allocations.import_allocation(
        document,
        document_digest="recovery-pool",
    )

    with session_factory() as session:
        records = session.scalars(
            select(AllocatedMacModel)
            .order_by(
                AllocatedMacModel.id
            )
        ).all()

        records[0].status = MacStatus.PROGRAMMING.value
        records[0].programming_started_at = (
            datetime.now(timezone.utc)
        )

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

        assert records[0].status == MacStatus.HOLD.value
        assert records[1].status == MacStatus.AVAILABLE.value


@pytest.mark.integration
def test_recovery_can_run_on_every_startup(
    persistence_stack,
) -> None:
    (
        _,
        session_factory,
        allocations,
        _,
        recovery,
    ) = persistence_stack

    document = make_allocation(
        allocation_id="RECOVERY-REPEAT",
        purpose=MacPurpose.STOCK_RF_TEST,
        start_value=0xAABBCCDDEE00,
        count=1,
    )

    allocations.import_allocation(
        document,
        document_digest="recovery-repeat",
    )

    with session_factory() as session:
        record = session.scalar(
            select(AllocatedMacModel)
        )

        assert record is not None

        record.status = MacStatus.PROGRAMMING.value

        session.commit()

    assert recovery.recover_uncertain_programming() == 1
    assert recovery.recover_uncertain_programming() == 0
    assert recovery.recover_uncertain_programming() == 0

