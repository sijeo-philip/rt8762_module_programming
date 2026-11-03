"""Session-bound repositories for the module QR registry and box reports.

Committed batches wait in the outbox until the box has recorded them.
Flush only; the caller's Unit of Work commits.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stationapp.data.models import (
    BatchModel,
    BatchSlotModel,
    ModuleQrRegistryModel,
    OutboxModel,
    SyncRunModel,
)

UPLOADED = "UPLOADED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QrRegistryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, module_qr: str) -> ModuleQrRegistryModel | None:
        return self._session.get(ModuleQrRegistryModel, module_qr)

    def add(self, *, module_qr: str, source: str, station_id: str, batch_id: str | None, slot_number: int | None, server_status: str, bound_at: datetime | None = None) -> ModuleQrRegistryModel:
        row = ModuleQrRegistryModel(
            module_qr=module_qr,
            source=source,
            station_id=station_id,
            batch_id=batch_id,
            slot_number=slot_number,
            bound_at=bound_at or utc_now(),
            server_status=server_status,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def rebind(self, *, module_qr: str, station_id: str, batch_id: str, slot_number: int, server_status: str, previous: str) -> ModuleQrRegistryModel:
        """A re-worked module: the QR now points at its new binding."""
        row = self.get(module_qr)
        if row is None:
            return self.add(module_qr=module_qr, source="LOCAL", station_id=station_id, batch_id=batch_id, slot_number=slot_number, server_status=server_status)
        row.source, row.station_id = "LOCAL", station_id
        row.batch_id, row.slot_number = batch_id, slot_number
        row.bound_at, row.server_status = utc_now(), server_status
        row.conflict_detail = f"Re-worked module; previously bound at {previous}"[:2000]
        self._session.flush()
        return row

    def set_status(self, module_qr: str, server_status: str, detail: str | None = None) -> None:
        row = self.get(module_qr)
        if row is not None:
            row.server_status = server_status
            row.conflict_detail = detail
            self._session.flush()

    def conflicts(self) -> list[ModuleQrRegistryModel]:
        return list(self._session.scalars(
            select(ModuleQrRegistryModel)
            .where(ModuleQrRegistryModel.server_status == "CONFLICT")
            .order_by(ModuleQrRegistryModel.bound_at.desc())
        ))

    def count_by_status(self, status: str) -> int:
        return int(self._session.scalar(
            select(func.count()).select_from(ModuleQrRegistryModel).where(
                ModuleQrRegistryModel.server_status == status,
                ModuleQrRegistryModel.source == "LOCAL",
            )
        ) or 0)


class ReportQueueRepository:
    """Committed batches (outbox rows) not yet recorded by the box."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def pending(self, limit: int | None = None) -> list[OutboxModel]:
        query = select(OutboxModel).where(OutboxModel.status != UPLOADED).order_by(OutboxModel.id)
        if limit is not None:
            query = query.limit(limit)
        return list(self._session.scalars(query))

    def mark_uploaded(self, outbox_rows: list[OutboxModel]) -> None:
        now = utc_now()
        batch_ids = [row.aggregate_id for row in outbox_rows if row.aggregate_type == "BATCH"]
        for row in outbox_rows:
            row.status = UPLOADED
            row.uploaded_at = now
            row.last_attempt_at = now
            row.attempt_count += 1
            row.last_error = None
        for batch in self._session.scalars(select(BatchModel).where(BatchModel.batch_id.in_(batch_ids))):
            if batch.state == "UPLOAD_PENDING":
                batch.state = "UPLOADED"
                batch.uploaded_at = now
        self._session.flush()

    def mark_failed(self, outbox_ids: list[int], error: str) -> None:
        now = utc_now()
        for row in self._session.scalars(select(OutboxModel).where(OutboxModel.id.in_(outbox_ids))):
            row.status = "RETRY"
            row.attempt_count += 1
            row.last_attempt_at = now
            row.last_error = error[:2000]
        self._session.flush()

    # -- run history -----------------------------------------------------

    def start_run(self, trigger: str) -> SyncRunModel:
        run = SyncRunModel(trigger=trigger, status="RUNNING", started_at=utc_now(), modules=0, batches=0, events=0, audits=0, qr_conflicts=0)
        self._session.add(run)
        self._session.flush()
        return run

    def last_runs(self, limit: int = 20) -> list[SyncRunModel]:
        return list(self._session.scalars(select(SyncRunModel).order_by(SyncRunModel.id.desc()).limit(limit)))

    def last_success(self) -> SyncRunModel | None:
        return self._session.scalar(
            select(SyncRunModel).where(SyncRunModel.status == "SUCCESS").order_by(SyncRunModel.id.desc()).limit(1)
        )


def module_qr_owner(session: Session, module_qr: str, *, ignore_batch: str | None = None) -> str | None:
    """Where a QR is already used, from the registry or batch history.

    ignore_batch: a registry row of that batch is the tray's own binding
    from before a restart (for a re-worked module, already approved).
    """
    entry = session.get(ModuleQrRegistryModel, module_qr)
    if entry is not None and ignore_batch is not None and entry.batch_id == ignore_batch:
        return None
    if entry is not None:
        origin = "this station" if entry.source == "LOCAL" else f"station {entry.station_id}"
        where = origin
        if entry.batch_id:
            where += f", batch {entry.batch_id}"
        if entry.slot_number:
            where += f", slot {entry.slot_number}"
        return where
    slot = session.scalar(select(BatchSlotModel).where(BatchSlotModel.module_qr == module_qr))
    if slot is not None:
        return f"this station, batch {slot.batch_id}, slot {slot.slot_number}"
    return None
