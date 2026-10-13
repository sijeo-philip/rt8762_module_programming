"""Session-bound repositories for the module QR registry and LAN sync.

Flush only; the caller's Unit of Work commits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stationapp.data.models import (
    AllocatedMacModel,
    AuditRecordModel,
    BatchModel,
    BatchSlotModel,
    ManufacturingEventModel,
    ModuleQrRegistryModel,
    OutboxModel,
    SyncRunModel,
    SyncStateModel,
)

UPLOADED = "UPLOADED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


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

    def for_batches(self, batch_ids: list[str]) -> list[ModuleQrRegistryModel]:
        if not batch_ids:
            return []
        return list(self._session.scalars(
            select(ModuleQrRegistryModel).where(
                ModuleQrRegistryModel.batch_id.in_(batch_ids),
                ModuleQrRegistryModel.source == "LOCAL",
            )
        ))

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


class SyncRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- cursors ---------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._session.get(SyncStateModel, key)
        return row.value if row is not None else default

    def set_state(self, key: str, value: str) -> None:
        row = self._session.get(SyncStateModel, key)
        if row is None:
            self._session.add(SyncStateModel(key=key, value=value))
        else:
            row.value = value
        self._session.flush()

    # -- pending work ----------------------------------------------------

    def pending_outbox(self, limit: int) -> list[OutboxModel]:
        return list(self._session.scalars(
            select(OutboxModel)
            .where(OutboxModel.status != UPLOADED)
            .order_by(OutboxModel.id)
            .limit(limit)
        ))

    def pending_modules(self) -> tuple[int, int]:
        """(populated modules, batches) waiting for upload."""
        modules = 0
        batches = 0
        for payload in self._session.scalars(select(OutboxModel.payload).where(OutboxModel.status != UPLOADED)):
            batches += 1
            modules += sum(1 for slot in payload.get("slots", []) if slot.get("disposition") != "EMPTY")
        return modules, batches

    def outbox_by_ids(self, ids: list[int]) -> list[OutboxModel]:
        if not ids:
            return []
        return list(self._session.scalars(select(OutboxModel).where(OutboxModel.id.in_(ids))))

    def events_after(self, last_id: int, limit: int) -> list[ManufacturingEventModel]:
        return list(self._session.scalars(
            select(ManufacturingEventModel)
            .where(ManufacturingEventModel.id > last_id)
            .order_by(ManufacturingEventModel.id)
            .limit(limit)
        ))

    def audits_after(self, last_id: int, limit: int) -> list[AuditRecordModel]:
        return list(self._session.scalars(
            select(AuditRecordModel)
            .where(AuditRecordModel.id > last_id)
            .order_by(AuditRecordModel.id)
            .limit(limit)
        ))

    def count_after(self, model, last_id: int) -> int:
        return int(self._session.scalar(select(func.count(model.id)).where(model.id > last_id)) or 0)

    def macs_for_batches(self, batch_ids: list[str]) -> list[AllocatedMacModel]:
        if not batch_ids:
            return []
        return list(self._session.scalars(
            select(AllocatedMacModel).where(AllocatedMacModel.batch_id.in_(batch_ids)).order_by(AllocatedMacModel.id)
        ))

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

    def mark_failed(self, outbox_ids: list[int], error: str, next_attempt_at: datetime) -> None:
        now = utc_now()
        for row in self._session.scalars(select(OutboxModel).where(OutboxModel.id.in_(outbox_ids))):
            row.status = "RETRY"
            row.attempt_count += 1
            row.last_attempt_at = now
            row.next_attempt_at = next_attempt_at
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


# -- serialisation for the sync package -----------------------------------


def event_json(row: ManufacturingEventModel) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "batch_id": row.batch_id,
        "slot_number": row.slot_number,
        "event_type": row.event_type,
        "correlation_id": row.correlation_id,
        "payload": row.payload,
        "occurred_at": _iso(row.occurred_at),
    }


def audit_json(row: AuditRecordModel) -> dict[str, Any]:
    return {
        "audit_id": row.audit_id,
        "user_id": row.user_id,
        "station_id": row.station_id,
        "record_type": row.record_type,
        "record_id": row.record_id,
        "action": row.action,
        "reason": row.reason,
        "previous_value": row.previous_value,
        "new_value": row.new_value,
        "created_at": _iso(row.created_at),
    }


def mac_json(row: AllocatedMacModel) -> dict[str, Any]:
    return {
        "address": row.address,
        "allocation_id": row.allocation_id,
        "purpose": row.purpose,
        "status": row.status,
        "batch_id": row.batch_id,
        "slot_number": row.slot_number,
        "module_qr": row.module_qr,
        "hold_reason": row.hold_reason,
        "reserved_at": _iso(row.reserved_at),
        "programming_started_at": _iso(row.programming_started_at),
        "issued_at": _iso(row.issued_at),
        "confirmed_at": _iso(row.confirmed_at),
        "held_at": _iso(row.held_at),
    }


def qr_json(row: ModuleQrRegistryModel) -> dict[str, Any]:
    return {
        "module_qr": row.module_qr,
        "batch_id": row.batch_id,
        "slot_number": row.slot_number,
        "bound_at": _iso(row.bound_at),
        "server_status": row.server_status,
    }


def module_qr_owner(session: Session, module_qr: str) -> str | None:
    """Where a QR is already used, from the registry or batch history."""
    entry = session.get(ModuleQrRegistryModel, module_qr)
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
