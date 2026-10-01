"""Transactional repositories for station production data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.errors import( AllocationImportConflict, BatchAlreadyExists, BatchNotFound, DuplicateMacInDatabase, DuplicateModuleQrInDatabase, InsufficientMacAvailability, 
                                   InvalidPersistedBatch, IncompleteSlotIdentity, NoQrSlotAvailable)

from stationapp.data.models import( AllocatedMacModel, AllocationModel, BatchModel, BatchSlotModel )
from stationapp.domain.allocation import AllocationDocument
from stationapp.domain.batch import Batch
from stationapp.domain.mac import AllocatedMac, MacAddress, MacPurpose, MacStatus
from stationapp.domain.slot import DeviceState

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class AllocationRepository:
    """Persistence operations for authorised server allocations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._session_factory = session_factory

    def import_allocation(self,  document: AllocationDocument, *, document_digest: str) -> bool:
        """Import one verified allocation document.

        Returns:
            True  - allocation was imported.
            False - the same document was already imported.

        A digest makes re-import idempotent.

        Global MAC uniqueness is checked explicitly and is also protected by
        the database UNIQUE constraint.
        """
        digest = document_digest.strip()
        if not digest:
            raise ValueError("document_digest cannot be empty")
        with self._session_factory() as session:
            try:
                existing_digest = session.scalar(
                    select(AllocationModel).where(
                        AllocationModel.document_digest == digest
                    )
                )

                if existing_digest is not None:
                    return False

                existing_allocation = session.get(
                    AllocationModel,
                    document.allocation_id,
                )

                if existing_allocation is not None:
                    raise AllocationImportConflict(
                        f"Allocation {document.allocation_id} already exists "
                        "with a different document digest"
                    )

                canonical_addresses = tuple(
                    address.compact
                    for address in document.addresses
                )

                duplicates = session.scalars(
                    select(AllocatedMacModel.address).where(
                        AllocatedMacModel.address.in_(canonical_addresses)
                    )
                ).all()

                if duplicates:
                    display = ", ".join(sorted(duplicates))

                    raise DuplicateMacInDatabase(
                        "Allocation contains MAC address(es) already known "
                        f"to this station: {display}"
                    )

                allocation = AllocationModel(
                    allocation_id=document.allocation_id,
                    station_id=document.station_id,
                    jig_id=document.jig_id,
                    purpose=document.purpose.value,
                    issued_at=document.issued_at,
                    expires_at=document.expires_at,
                    schema_version=document.schema_version,
                    document_digest=digest,
                    imported_at=utc_now(),
                )

                session.add(allocation)

                for address in document.addresses:
                    session.add(
                        AllocatedMacModel(
                            allocation_id=document.allocation_id,
                            address=address.compact,
                            purpose=document.purpose.value,
                            status=MacStatus.AVAILABLE.value,
                        )
                    )

                session.commit()
                return True

            except IntegrityError as exc:
                session.rollback()

                raise DuplicateMacInDatabase(
                    "Database rejected the allocation because a globally "
                    "unique value already exists"
                ) from exc


class BatchRepository:
    """Persistence operations for production batches."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_batch(self, batch: Batch) -> None:
        """Persist a new batch and all of its physical jig slots atomically."""

        if batch.slot_count not in (4, 8):
            raise InvalidPersistedBatch(
                "A production batch must contain exactly 4 or 8 slots"
            )

        with self._session_factory() as session:
            try:
                existing = session.get(
                    BatchModel,
                    batch.batch_id,
                )

                if existing is not None:
                    raise BatchAlreadyExists(
                        f"Batch {batch.batch_id} already exists"
                    )

                batch_model = BatchModel(
                    batch_id=batch.batch_id,
                    station_id=batch.station_id,
                    jig_id=batch.jig_id,
                    slot_count=batch.slot_count,
                    state=batch.state.value,
                    hold_reason=batch.hold_reason,
                    created_at=batch.created_at,
                )

                session.add(batch_model)

                for slot in batch.ordered_slots:
                    session.add(
                        BatchSlotModel(
                            batch_id=batch.batch_id,
                            slot_number=slot.number,
                            port_identity=slot.port_identity,
                            device_state=slot.state.value,
                            module_qr=slot.module_qr,
                            hold_reason=slot.hold_reason,
                        )
                    )

                session.commit()

            except IntegrityError:
                session.rollback()
                raise

    def _load_batch_slot_numbers(self, session: Session, batch_id: str) -> tuple[int, ...]:
        rows = session.scalars(select(BatchSlotModel.slot_number).where(BatchSlotModel.batch_id == batch_id).order_by(BatchSlotModel.slot_number)).all()
        return tuple(rows)

    def _validate_persisted_slots(self, session: Session, batch: BatchModel) -> tuple[int, ...]:
        slots = self._load_batch_slot_numbers(session, batch.batch_id)
        expected = tuple(range(1, batch.slot_count + 1))
        if slots != expected:
            raise InvalidPersistedBatch(f"Batch {batch.batch_id} expected slots "
                f"{expected}, found {slots}"
            )

        return slots

    def reserve_macs(self, *, batch_id: str, purpose: MacPurpose) -> tuple[AllocatedMac, ...]:
        """Reserve one complete jig load atomically.

        SQLite BEGIN IMMEDIATE obtains the write reservation before availability
        is inspected.  Another writer therefore cannot select the same set of
        AVAILABLE addresses between our SELECT and UPDATE operations.

        Either every physical slot receives one unique MAC, or the transaction
        rolls back and no MAC is consumed.
        """

        session = self._session_factory()

        try:
            # This must be the first database operation in this transaction.
            #
            # BEGIN IMMEDIATE acquires SQLite's reserved writer lock now,
            # rather than waiting until the first UPDATE.
            session.execute(
                text("BEGIN IMMEDIATE")
            )

            batch = session.get(
                BatchModel,
                batch_id,
            )

            if batch is None:
                raise BatchNotFound(
                    f"Batch {batch_id} does not exist"
                )

            slot_numbers = self._validate_persisted_slots(
                session,
                batch,
            )

            required_count = len(slot_numbers)

            available = session.scalars(
                select(AllocatedMacModel)
                .where(
                    AllocatedMacModel.purpose == purpose.value,
                    AllocatedMacModel.status == MacStatus.AVAILABLE.value,
                )
                .order_by(
                    AllocatedMacModel.id
                )
                .limit(required_count)
            ).all()

            if len(available) != required_count:
                raise InsufficientMacAvailability(
                    f"Batch {batch_id} requires {required_count} "
                    f"{purpose.value} MACs; only {len(available)} "
                    "are available"
                )

            now = utc_now()

            reserved_domain_records: list[AllocatedMac] = []

            for slot_number, model in zip(
                slot_numbers,
                available,
                strict=True,
            ):
                model.status = MacStatus.RESERVED.value
                model.batch_id = batch_id
                model.slot_number = slot_number
                model.reserved_at = now

                domain_record = AllocatedMac(
                    allocation_id=model.allocation_id,
                    address=MacAddress.parse(model.address),
                    purpose=purpose,
                    status=MacStatus.RESERVED,
                    batch_id=batch_id,
                    slot_number=slot_number,
                )

                reserved_domain_records.append(
                    domain_record
                )

            session.flush()
            session.commit()

            return tuple(reserved_domain_records)

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

    def bind_next_module_qr(self, *, batch_id: str, module_qr: str) -> int:
        """Bind scanner input to the next physical slot.

        The scanner supplies only the module QR.  Slot identity comes from the
        persisted ordering of the batch.

        The operation is atomic:

        * bind the QR to the expected batch slot;
        * propagate it to the stock MAC record;
        * propagate it to the Pricol MAC record.

        If any required identity record is missing, nothing is changed.

        Returns:
            Physical slot number that received the QR.
        """

        qr = module_qr.strip()

        if not qr:
            raise ValueError("module_qr cannot be empty")

        session = self._session_factory()

        try:
            # Obtain SQLite writer ownership before reading the next QR slot.
            session.execute(
                text("BEGIN IMMEDIATE")
            )

            batch = session.get(
                BatchModel,
                batch_id,
            )

            if batch is None:
                raise BatchNotFound(
                    f"Batch {batch_id} does not exist"
                )

            self._validate_persisted_slots(
                session,
                batch,
            )

            # Reject a QR already bound anywhere in station history.
            existing_qr = session.scalar(
                select(BatchSlotModel).where(
                    BatchSlotModel.module_qr == qr
                )
            )

            if existing_qr is not None:
                raise DuplicateModuleQrInDatabase(
                    f"Module QR {qr} is already linked to "
                    f"batch {existing_qr.batch_id}, "
                    f"slot {existing_qr.slot_number}"
                )

            slots = session.scalars(
                select(BatchSlotModel)
                .where(
                    BatchSlotModel.batch_id == batch_id
                )
                .order_by(
                    BatchSlotModel.slot_number
                )
            ).all()

            next_slot = next(
                (
                    slot
                    for slot in slots
                    if slot.module_qr is None
                ),
                None,
            )

            if next_slot is None:
                raise NoQrSlotAvailable(
                    f"All module QRs for batch {batch_id} "
                    "have already been bound"
                )

            mac_records = session.scalars(
                select(AllocatedMacModel)
                .where(
                    AllocatedMacModel.batch_id == batch_id,
                    AllocatedMacModel.slot_number
                    == next_slot.slot_number,
                )
            ).all()

            by_purpose = {
                record.purpose: record
                for record in mac_records
            }

            required_purposes = {
                MacPurpose.STOCK_RF_TEST.value,
                MacPurpose.PRICOL_PRODUCTION.value,
            }

            if set(by_purpose) != required_purposes:
                raise IncompleteSlotIdentity(
                    f"Batch {batch_id} slot "
                    f"{next_slot.slot_number} does not contain exactly "
                    "one stock MAC and one Pricol MAC"
                )

            next_slot.module_qr = qr

            # QR_BOUND is the persisted slot state once permanent module
            # identity has been attached.
            next_slot.device_state = DeviceState.QR_BOUND.value

            by_purpose[
                MacPurpose.STOCK_RF_TEST.value
            ].module_qr = qr

            by_purpose[
                MacPurpose.PRICOL_PRODUCTION.value
            ].module_qr = qr

            session.flush()
            session.commit()

            return next_slot.slot_number

        except IntegrityError as exc:
            session.rollback()

            raise DuplicateModuleQrInDatabase(
                f"Database rejected module QR {qr} because it "
                "already exists"
            ) from exc

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

class RecoveryRepository:
    """Startup repair operations for uncertain persisted state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def recover_uncertain_programming(self) -> int:
        """Quarantine every MAC left in PROGRAMMING.

        When the application restarts it cannot know whether the external
        programmer completed the physical write before the interruption.

        Such a MAC therefore becomes HOLD rather than AVAILABLE.

        Returns:
            Number of MAC records moved to HOLD.
        """

        session = self._session_factory()

        try:
            session.execute(
                text("BEGIN IMMEDIATE")
            )

            records = session.scalars(
                select(AllocatedMacModel)
                .where(
                    AllocatedMacModel.status
                    == MacStatus.PROGRAMMING.value
                )
                .order_by(
                    AllocatedMacModel.id
                )
            ).all()

            if not records:
                session.commit()
                return 0

            now = utc_now()

            for record in records:
                record.status = MacStatus.HOLD.value

                record.hold_reason = (
                    "Startup recovery: MAC was left in PROGRAMMING "
                    "state; programming outcome is uncertain"
                )

                record.held_at = now

            session.flush()
            session.commit()

            return len(records)

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()