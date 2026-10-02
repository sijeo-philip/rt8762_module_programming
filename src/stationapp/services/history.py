"""Manufacturing genealogy reconstruction services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.event_repository import (
    ManufacturingEventRepository,
)
from stationapp.data.models import BatchSlotModel
from stationapp.domain.events import (
    EventType,
    ManufacturingEvent,
)


@dataclass(frozen=True,slots=True)
class DeviceHistoryEntry:
    event_id: str
    event_type: EventType
    batch_id: str
    slot_number: int | None
    occurred_at: datetime
    payload: dict


@dataclass(frozen=True, slots=True)
class DeviceHistory:
    module_qr: str | None
    batch_id: str
    slot_number: int
    events: tuple[DeviceHistoryEntry, ...]


class DeviceHistoryService:
    """Reconstruct manufacturing genealogy from append-only events."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def for_batch_slot(self, *, batch_id: str, slot_number: int) -> DeviceHistory:

        if slot_number <= 0:
            raise ValueError(
                "slot_number must be positive"
            )

        with self._session_factory() as session:
            slot = session.scalar(
                select(
                    BatchSlotModel
                ).where(
                    BatchSlotModel.batch_id
                    == batch_id,
                    BatchSlotModel.slot_number
                    == slot_number,
                )
            )

            if slot is None:
                raise LookupError(
                    f"Batch {batch_id} slot "
                    f"{slot_number} does not exist"
                )

            repository = ManufacturingEventRepository(
                session
            )

            batch_events = repository.list_for_batch(
                batch_id
            )

            relevant = tuple(
                event
                for event in batch_events
                if (
                    event.slot_number
                    in {
                        None,
                        slot_number,
                    }
                )
            )

            return DeviceHistory(
                module_qr=slot.module_qr,
                batch_id=batch_id,
                slot_number=slot_number,
                events=tuple(
                    self._entry(event)
                    for event in relevant
                ),
            )

    def for_module_qr(self, module_qr: str) -> DeviceHistory:
        qr = module_qr.strip()

        if not qr:
            raise ValueError(
                "module_qr cannot be empty"
            )

        with self._session_factory() as session:
            slot = session.scalar(
                select(
                    BatchSlotModel
                ).where(
                    BatchSlotModel.module_qr
                    == qr
                )
            )
            if slot is None:
                raise LookupError(
                    f"Module QR {qr} is not known "
                    "to this station"
                )
            batch_id = slot.batch_id
            slot_number = slot.slot_number

        return self.for_batch_slot(
            batch_id=batch_id,
            slot_number=slot_number,
        )

    @staticmethod
    def _entry(event: ManufacturingEvent) -> DeviceHistoryEntry:

        return DeviceHistoryEntry(
            event_id=event.event_id,
            event_type=event.event_type,
            batch_id=event.batch_id,
            slot_number=event.slot_number,
            occurred_at=event.occurred_at,
            payload=dict(
                event.payload
            ),
        )

    