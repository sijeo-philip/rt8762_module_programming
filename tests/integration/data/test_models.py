"""Integration tests for the station persistence schema."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stationapp.data.base import Base
from stationapp.data.database import create_station_engine
from stationapp.data.models import (
    AllocatedMacModel,
    AllocationModel,
    BatchModel,
    BatchSlotModel,
)


@pytest.fixture
def database_engine(tmp_path: Path):
    database_file = tmp_path / "station.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    # create_all() is acceptable here because this is an isolated schema test.
    # Production schema deployment will use Alembic in Lesson 5C.
    Base.metadata.create_all(engine)

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.integration
def test_expected_tables_exist(database_engine) -> None:
    inspector = inspect(database_engine)

    tables = set(inspector.get_table_names())

    assert {
        "allocations",
        "allocated_macs",
        "batches",
        "batch_slots",
        "programming_events",
        "verification_events",
        "manufacturing_events",
        "audit_records",
        "operation_logs",
        "outbox",
       
    }.issubset(tables)


@pytest.mark.integration
def test_mac_address_is_globally_unique(database_engine) -> None:
    with Session(database_engine) as session:
        allocation_one = AllocationModel(
            allocation_id="ALLOC-STOCK-001",
            station_id="STATION-01",
            jig_id="JIG-01",
            purpose="STOCK_RF_TEST",
            issued_at=datetime.now(timezone.utc),
            document_digest="digest-one",
        )

        allocation_two = AllocationModel(
            allocation_id="ALLOC-STOCK-002",
            station_id="STATION-01",
            jig_id="JIG-01",
            purpose="STOCK_RF_TEST",
            issued_at=datetime.now(timezone.utc),
            document_digest="digest-two",
        )

        session.add_all([allocation_one, allocation_two])
        session.flush()

        first = AllocatedMacModel(
            allocation_id="ALLOC-STOCK-001",
            address="AABBCCDDEE01",
            purpose="STOCK_RF_TEST",
            status="AVAILABLE",
        )

        duplicate = AllocatedMacModel(
            allocation_id="ALLOC-STOCK-002",
            address="AABBCCDDEE01",
            purpose="STOCK_RF_TEST",
            status="AVAILABLE",
        )

        session.add(first)
        session.flush()

        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.flush()

        session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("slot_count", [4, 8])
def test_batch_accepts_supported_slot_counts(
    database_engine,
    slot_count: int,
) -> None:
    with Session(database_engine) as session:
        batch = BatchModel(
            batch_id=f"BATCH-{slot_count}",
            station_id="STATION-01",
            jig_id="JIG-01",
            slot_count=slot_count,
            state="CREATED",
        )

        session.add(batch)
        session.commit()


@pytest.mark.integration
def test_batch_rejects_invalid_slot_count(database_engine) -> None:
    with Session(database_engine) as session:
        batch = BatchModel(
            batch_id="BATCH-INVALID",
            station_id="STATION-01",
            jig_id="JIG-01",
            slot_count=6,
            state="CREATED",
        )

        session.add(batch)

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()


@pytest.mark.integration
def test_same_slot_cannot_exist_twice_in_batch(
    database_engine,
) -> None:
    with Session(database_engine) as session:
        batch = BatchModel(
            batch_id="BATCH-001",
            station_id="STATION-01",
            jig_id="JIG-01",
            slot_count=4,
            state="CREATED",
        )

        session.add(batch)
        session.flush()

        first = BatchSlotModel(
            batch_id=batch.batch_id,
            slot_number=1,
            device_state="EMPTY",
        )

        duplicate = BatchSlotModel(
            batch_id=batch.batch_id,
            slot_number=1,
            device_state="EMPTY",
        )

        session.add(first)
        session.flush()

        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.flush()

        session.rollback()


@pytest.mark.integration
def test_module_qr_is_globally_unique(database_engine) -> None:
    with Session(database_engine) as session:
        batch = BatchModel(
            batch_id="BATCH-QR",
            station_id="STATION-01",
            jig_id="JIG-01",
            slot_count=4,
            state="QR_SCANNING",
        )

        session.add(batch)
        session.flush()

        slot_one = BatchSlotModel(
            batch_id=batch.batch_id,
            slot_number=1,
            module_qr="MODULE-0001",
            device_state="QR_BOUND",
        )

        slot_two = BatchSlotModel(
            batch_id=batch.batch_id,
            slot_number=2,
            module_qr="MODULE-0001",
            device_state="QR_BOUND",
        )

        session.add(slot_one)
        session.flush()

        session.add(slot_two)

        with pytest.raises(IntegrityError):
            session.flush()

        session.rollback()

@pytest.mark.integration
def test_mac_cannot_reference_missing_allocation(
    database_engine,
) -> None:
    with Session(database_engine) as session:
        mac = AllocatedMacModel(
            allocation_id="DOES-NOT-EXIST",
            address="AABBCCDDEE99",
            purpose="STOCK_RF_TEST",
            status="AVAILABLE",
        )

        session.add(mac)

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

@pytest.mark.integration
def test_slot_cannot_receive_two_macs_for_same_purpose(
    database_engine,
) -> None:
    with Session(database_engine) as session:
        allocation = AllocationModel(
            allocation_id="ALLOC-001",
            station_id="STATION-01",
            jig_id="JIG-01",
            purpose="STOCK_RF_TEST",
            issued_at=datetime.now(timezone.utc),
            document_digest="digest-slot-purpose",
        )

        batch = BatchModel(
            batch_id="BATCH-PURPOSE",
            station_id="STATION-01",
            jig_id="JIG-01",
            slot_count=4,
            state="PORTS_BOUND",
        )

        session.add_all([allocation, batch])
        session.flush()

        first = AllocatedMacModel(
            allocation_id=allocation.allocation_id,
            address="AABBCCDDEE10",
            purpose="STOCK_RF_TEST",
            status="RESERVED",
            batch_id=batch.batch_id,
            slot_number=1,
        )

        second = AllocatedMacModel(
            allocation_id=allocation.allocation_id,
            address="AABBCCDDEE11",
            purpose="STOCK_RF_TEST",
            status="RESERVED",
            batch_id=batch.batch_id,
            slot_number=1,
        )

        session.add(first)
        session.flush()

        session.add(second)

        with pytest.raises(IntegrityError):
            session.flush()

        session.rollback()



