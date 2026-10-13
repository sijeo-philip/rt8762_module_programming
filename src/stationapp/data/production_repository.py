"""Persistence of live production state and the upload outbox.

The domain Batch aggregate is the source of truth while a batch runs.
After every workflow step the orchestrator writes a snapshot of the
batch, its physical slots and every MAC it touched, together with the
append-only manufacturing events for that step, in one Unit of Work.

Writing MAC status as it changes is what makes startup recovery work:
RecoveryRepository quarantines any MAC still recorded as PROGRAMMING.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.errors import InvalidPersistedBatch
from stationapp.data.models import (
    AllocatedMacModel,
    BatchModel,
    BatchSlotModel,
    OutboxModel,
)
from stationapp.data.repositories import BatchRepository
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.batch import Batch, BatchState
from stationapp.domain.events import ManufacturingEvent
from stationapp.domain.mac import AllocatedMac, MacPurpose, MacStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_COMMITTED_STATES = frozenset({
    BatchState.COMMITTED,
    BatchState.UPLOAD_PENDING,
    BatchState.UPLOADED,
})

_STATUS_TIMESTAMP = {
    MacStatus.PROGRAMMING: "programming_started_at",
    MacStatus.ISSUED: "issued_at",
    MacStatus.CONFIRMED: "confirmed_at",
    MacStatus.HOLD: "held_at",
}


class ProductionStateRepository:
    """Session-bound writer for batch, slot and MAC state.

    Flushes but never commits; the caller's Unit of Work owns the
    transaction.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def sync_batch(self, batch: Batch) -> None:
        model = self._session.get(BatchModel, batch.batch_id)

        if model is None:
            raise InvalidPersistedBatch(f"Batch {batch.batch_id} was never persisted")

        model.state = batch.state.value
        model.hold_reason = batch.hold_reason

        if batch.state in _COMMITTED_STATES and model.committed_at is None:
            model.committed_at = utc_now()

        if batch.state is BatchState.UPLOADED and model.uploaded_at is None:
            model.uploaded_at = utc_now()

        rows = {row.slot_number: row for row in model.slots}

        for slot in batch.ordered_slots:
            row = rows.get(slot.number)
            if row is None:
                raise InvalidPersistedBatch(
                    f"Batch {batch.batch_id} has no persisted slot {slot.number}"
                )

            row.port_identity = slot.port_identity
            row.device_state = slot.state.value
            row.module_qr = slot.module_qr
            row.pricol_app_passed = slot.functional.pricol_app_passed
            row.pricol_dfu_passed = slot.functional.pricol_dfu_passed
            row.hold_reason = slot.hold_reason

            for record in (slot.stock_mac_record, slot.pricol_mac_record):
                if record is not None:
                    self.sync_mac(record)

        self._session.flush()

    def sync_mac(self, record: AllocatedMac) -> None:
        row = self._session.scalar(
            select(AllocatedMacModel).where(
                AllocatedMacModel.address == record.address.compact
            )
        )

        if row is None:
            raise InvalidPersistedBatch(
                f"MAC {record.address} is not in the local authorized cache"
            )

        if row.status != record.status.value:
            column = _STATUS_TIMESTAMP.get(record.status)
            if column is not None and getattr(row, column) is None:
                setattr(row, column, utc_now())

        row.status = record.status.value
        row.batch_id = record.batch_id
        row.slot_number = record.slot_number
        row.module_qr = record.module_qr
        row.hold_reason = record.hold_reason

    def count_available(self, purpose: MacPurpose) -> int:
        return int(
            self._session.scalar(
                select(func.count(AllocatedMacModel.id)).where(
                    AllocatedMacModel.purpose == purpose.value,
                    AllocatedMacModel.status == MacStatus.AVAILABLE.value,
                )
            )
            or 0
        )

    def module_qr_in_use(self, module_qr: str) -> bool:
        return (
            self._session.scalar(
                select(BatchSlotModel.id).where(BatchSlotModel.module_qr == module_qr.strip())
            )
            is not None
        )

    def enqueue_upload(self, *, aggregate_type: str, aggregate_id: str, payload: dict[str, Any]) -> str:
        transaction_id = str(uuid4())
        self._session.add(
            OutboxModel(
                client_transaction_id=transaction_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                status="PENDING",
                attempt_count=0,
                created_at=utc_now(),
                next_attempt_at=utc_now(),
            )
        )
        self._session.flush()
        return transaction_id

    def pending_upload_count(self) -> int:
        return int(
            self._session.scalar(
                select(func.count(OutboxModel.id)).where(OutboxModel.status != "UPLOADED")
            )
            or 0
        )


class SqlProductionStore:
    """Transactional persistence used by the production orchestrator."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._batches = BatchRepository(session_factory)

    def create(self, batch: Batch, events: Sequence[ManufacturingEvent] = ()) -> None:
        self._batches.create_batch(batch)
        self.save(batch, events)

    def reserve_macs(self, batch: Batch, purpose: MacPurpose, slot_numbers: tuple[int, ...]) -> tuple[AllocatedMac, ...]:
        return self._batches.reserve_macs(
            batch_id=batch.batch_id,
            purpose=purpose,
            slot_numbers=slot_numbers,
        )

    def save(self, batch: Batch, events: Iterable[ManufacturingEvent] = ()) -> None:
        with UnitOfWork(self._session_factory) as uow:
            ProductionStateRepository(uow.session).sync_batch(batch)
            for event in events:
                uow.events.append(event)
            uow.commit()

    def save_and_enqueue(self, batch: Batch, events: Iterable[ManufacturingEvent], upload_payload: dict[str, Any]) -> str:
        """Commit final batch state and queue its upload atomically."""
        with UnitOfWork(self._session_factory) as uow:
            repository = ProductionStateRepository(uow.session)
            repository.sync_batch(batch)
            for event in events:
                uow.events.append(event)
            transaction_id = repository.enqueue_upload(
                aggregate_type="BATCH",
                aggregate_id=batch.batch_id,
                payload=upload_payload,
            )
            uow.commit()
            return transaction_id

    def module_qr_in_use(self, module_qr: str) -> bool:
        return self.module_qr_owner(module_qr) is not None

    def module_qr_owner(self, module_qr: str) -> str | None:
        """Where the QR is already used: this station's history, or
        another station as reported by the LAN server."""
        from stationapp.data.sync_repository import module_qr_owner

        with self._session_factory() as session:
            return module_qr_owner(session, module_qr.strip())

    def bind_module_qr(self, batch: Batch, events: Iterable[ManufacturingEvent], *, module_qr: str, slot_number: int, server_status: str) -> None:
        """Persist a QR binding and its registry entry in one transaction.

        The registry primary key makes a second binding of the same QR
        fail the whole transaction.
        """
        from stationapp.data.sync_repository import QrRegistryRepository

        with UnitOfWork(self._session_factory) as uow:
            QrRegistryRepository(uow.session).add(
                module_qr=module_qr,
                source="LOCAL",
                station_id=batch.station_id,
                batch_id=batch.batch_id,
                slot_number=slot_number,
                server_status=server_status,
            )
            ProductionStateRepository(uow.session).sync_batch(batch)
            for event in events:
                uow.events.append(event)
            uow.commit()

    def available_count(self, purpose: MacPurpose) -> int:
        with self._session_factory() as session:
            return ProductionStateRepository(session).count_available(purpose)

    def pending_upload_count(self) -> int:
        with self._session_factory() as session:
            return ProductionStateRepository(session).pending_upload_count()
