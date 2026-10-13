"""Synchronize the local station database with the LAN server.

What is sent (README "Session Completion and Server Synchronization"):
completed batch records with PASS/FAIL/HOLD dispositions, programming
and test history (manufacturing events), MAC consumption, QR
associations, and audit records.

When:
* automatically once SYNC_MODULE_THRESHOLD completed modules (default
  1000) are waiting for upload;
* on demand ("Sync now") and before a Supervisor closes the session;
* on retry, while the threshold is still exceeded, after a failure.

Sync runs on a background thread and never blocks production. Data is
sent in packages; local cursors and outbox rows advance only after the
server acknowledges a package, so an interrupted sync resumes where it
stopped. Every record carries its natural unique ID (batch_id, event_id,
audit_id, MAC address), so a package re-sent after a lost
acknowledgement is idempotent on the server.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.models import AuditRecordModel, ManufacturingEventModel
from stationapp.data.sync_repository import (
    QrRegistryRepository,
    SyncRepository,
    audit_json,
    event_json,
    mac_json,
    qr_json,
)
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.events import EventType, ManufacturingEvent
from stationapp.drivers.lan.client import LanServerClient, LanServerError, QrOwner

logger = logging.getLogger(__name__)

LAST_EVENT_ID = "last_event_id"
LAST_AUDIT_ID = "last_audit_id"
QR_CURSOR = "module_qr_cursor"
PACKAGE_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SyncInProgress(Exception):
    """A sync is already running."""


@dataclass(frozen=True, slots=True)
class SyncReport:
    trigger: str
    success: bool
    modules: int = 0
    batches: int = 0
    events: int = 0
    audits: int = 0
    qr_conflicts: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SyncStatus:
    configured: bool
    running: bool
    threshold: int
    pending_modules: int
    pending_batches: int
    unsynced_events: int
    unsynced_audits: int
    unconfirmed_qr: int
    qr_conflicts: int
    last_success_at: datetime | None
    last_error: str | None

    @property
    def threshold_reached(self) -> bool:
        return self.pending_modules >= self.threshold


class ServerSyncService:
    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, jig_id: str, client: LanServerClient | None, threshold_modules: int = 1000, batches_per_package: int = 50, records_per_package: int = 2000) -> None:
        if threshold_modules < 1:
            raise ValueError("threshold_modules must be positive")
        self._session_factory = session_factory
        self._station_id = station_id
        self._jig_id = jig_id
        self._client = client
        self._threshold = threshold_modules
        self._batches_per_package = batches_per_package
        self._records_per_package = records_per_package
        self._lock = threading.Lock()
        self._running = False

    @property
    def configured(self) -> bool:
        return self._client is not None

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------

    def status(self) -> SyncStatus:
        with self._session_factory() as session:
            sync = SyncRepository(session)
            registry = QrRegistryRepository(session)
            modules, batches = sync.pending_modules()
            last_event = int(sync.get_state(LAST_EVENT_ID, "0"))
            last_audit = int(sync.get_state(LAST_AUDIT_ID, "0"))
            success = sync.last_success()
            runs = sync.last_runs(1)
            last_error = runs[0].detail if runs and runs[0].status == "FAILED" else None
            return SyncStatus(
                configured=self.configured,
                running=self._running,
                threshold=self._threshold,
                pending_modules=modules,
                pending_batches=batches,
                unsynced_events=sync.count_after(ManufacturingEventModel, last_event),
                unsynced_audits=sync.count_after(AuditRecordModel, last_audit),
                unconfirmed_qr=registry.count_by_status("UNCONFIRMED"),
                qr_conflicts=registry.count_by_status("CONFLICT"),
                last_success_at=_aware(success.finished_at) if success else None,
                last_error=last_error,
            )

    def threshold_reached(self) -> bool:
        with self._session_factory() as session:
            modules, _ = SyncRepository(session).pending_modules()
        return modules >= self._threshold

    def conflicts(self) -> list[dict]:
        with self._session_factory() as session:
            return [
                {"module_qr": row.module_qr, "batch_id": row.batch_id, "slot_number": row.slot_number, "detail": row.conflict_detail}
                for row in QrRegistryRepository(session).conflicts()
            ]

    # ------------------------------------------------------------------

    def sync(self, trigger: str) -> SyncReport:
        with self._lock:
            if self._running:
                raise SyncInProgress("A sync is already running")
            self._running = True

        try:
            return self._sync(trigger)
        finally:
            with self._lock:
                self._running = False

    def _sync(self, trigger: str) -> SyncReport:
        if self._client is None:
            return SyncReport(trigger, False, detail="LAN server not configured")

        with UnitOfWork(self._session_factory) as uow:
            run_id = SyncRepository(uow.session).start_run(trigger).id
            uow.commit()

        totals = {"modules": 0, "batches": 0, "events": 0, "audits": 0, "qr_conflicts": 0}
        current_outbox: list[int] = []
        logger.info("LAN sync started | trigger=%s", trigger)

        try:
            totals["qr_conflicts"] += self._pull_known_qr()

            while True:
                package, current_outbox, counts = self._build_package()
                if package is None:
                    break

                ack = self._client.upload_sync_package({key: value for key, value in package.items() if not key.startswith("_")})
                totals["qr_conflicts"] += self._acknowledge(package, current_outbox, ack.qr_conflicts)
                for key in ("modules", "batches", "events", "audits"):
                    totals[key] += counts[key]
                current_outbox = []

        except LanServerError as exc:
            detail = str(exc)
            logger.warning("LAN sync failed | trigger=%s error=%s", trigger, detail)
            self._finish_run(run_id, "FAILED", totals, detail, failed_outbox=current_outbox)
            return SyncReport(trigger, False, detail=detail, **totals)
        except Exception as exc:  # noqa: BLE001 - recorded; production continues
            logger.exception("LAN sync crashed | trigger=%s", trigger)
            detail = f"Unexpected sync error: {exc}"
            self._finish_run(run_id, "FAILED", totals, detail, failed_outbox=current_outbox)
            return SyncReport(trigger, False, detail=detail, **totals)

        detail = (
            f"Synced {totals['modules']} modules in {totals['batches']} batches, "
            f"{totals['events']} events, {totals['audits']} audit records"
        )
        if totals["qr_conflicts"]:
            detail += f"; {totals['qr_conflicts']} QR conflict(s) need Supervisor review"
        self._finish_run(run_id, "SUCCESS", totals, detail)
        logger.info("LAN sync finished | %s", detail)
        return SyncReport(trigger, True, detail=detail, **totals)

    # ------------------------------------------------------------------

    def _pull_known_qr(self) -> int:
        """Import QR codes bound anywhere on the line; return new conflicts."""
        assert self._client is not None
        conflicts = 0
        with self._session_factory() as session:
            cursor = SyncRepository(session).get_state(QR_CURSOR)

        while True:
            codes, next_cursor = self._client.fetch_module_qr(since=cursor)
            with UnitOfWork(self._session_factory) as uow:
                registry = QrRegistryRepository(uow.session)
                events = []
                for known in codes:
                    conflict = self._merge_known(registry, known.module_qr, known.owner)
                    if conflict is not None:
                        conflicts += 1
                        events.append(conflict)
                for event in events:
                    uow.events.append(event)
                if next_cursor:
                    SyncRepository(uow.session).set_state(QR_CURSOR, next_cursor)
                uow.commit()
            if not codes or not next_cursor or next_cursor == cursor:
                return conflicts
            cursor = next_cursor

    def _merge_known(self, registry: QrRegistryRepository, module_qr: str, owner: QrOwner) -> ManufacturingEvent | None:
        row = registry.get(module_qr)
        if row is None:
            registry.add(
                module_qr=module_qr,
                source="SERVER",
                station_id=owner.station_id,
                batch_id=owner.batch_id,
                slot_number=owner.slot_number,
                server_status="SYNCED",
            )
            return None
        if row.source != "LOCAL":
            return None
        if owner.station_id == self._station_id and owner.batch_id in (None, row.batch_id):
            if row.server_status != "CONFLICT":
                row.server_status = "SYNCED"
            return None
        return self._mark_conflict(row, owner)

    def _mark_conflict(self, row, owner: QrOwner) -> ManufacturingEvent | None:
        if row.server_status == "CONFLICT":
            return None
        row.server_status = "CONFLICT"
        row.conflict_detail = f"Also bound at {owner.describe()}"
        logger.error("Duplicate module QR detected by LAN server | qr=%s local_batch=%s other=%s", row.module_qr, row.batch_id, owner.describe())
        if not row.batch_id:
            return None
        return ManufacturingEvent(
            event_type=EventType.QR_CONFLICT_REPORTED,
            batch_id=row.batch_id,
            station_id=self._station_id,
            jig_id=self._jig_id,
            slot_number=row.slot_number,
            module_qr=row.module_qr,
            payload={"other_station_id": owner.station_id, "other_batch_id": owner.batch_id, "other_slot_number": owner.slot_number, "other_bound_at": owner.bound_at},
        )

    def _build_package(self):
        with self._session_factory() as session:
            sync = SyncRepository(session)
            outbox = sync.pending_outbox(self._batches_per_package)
            last_event = int(sync.get_state(LAST_EVENT_ID, "0"))
            last_audit = int(sync.get_state(LAST_AUDIT_ID, "0"))
            events = sync.events_after(last_event, self._records_per_package)
            audits = sync.audits_after(last_audit, self._records_per_package)

            if not outbox and not events and not audits:
                return None, [], {}

            batch_ids = [row.aggregate_id for row in outbox if row.aggregate_type == "BATCH"]
            batches = [row.payload for row in outbox]
            package = {
                "schema_version": PACKAGE_SCHEMA_VERSION,
                "client_transaction_id": str(uuid4()),
                "station_id": self._station_id,
                "jig_id": self._jig_id,
                "sent_at": _utc_now().isoformat(),
                "batches": batches,
                "macs": [mac_json(row) for row in sync.macs_for_batches(batch_ids)],
                "module_qr": [qr_json(row) for row in QrRegistryRepository(session).for_batches(batch_ids)],
                "events": [event_json(row) for row in events],
                "audits": [audit_json(row) for row in audits],
                "_cursor": {
                    LAST_EVENT_ID: events[-1].id if events else last_event,
                    LAST_AUDIT_ID: audits[-1].id if audits else last_audit,
                },
            }
            counts = {
                "modules": sum(1 for batch in batches for slot in batch.get("slots", []) if slot.get("disposition") != "EMPTY"),
                "batches": len(batches),
                "events": len(events),
                "audits": len(audits),
            }
            return package, [row.id for row in outbox], counts

    def _acknowledge(self, package: dict, outbox_ids: list[int], conflicts) -> int:
        cursor = package["_cursor"]
        conflicted = {qr: owner for qr, owner in conflicts}
        new_conflicts = 0
        with UnitOfWork(self._session_factory) as uow:
            sync = SyncRepository(uow.session)
            registry = QrRegistryRepository(uow.session)
            sync.mark_uploaded(sync.outbox_by_ids(outbox_ids))
            sync.set_state(LAST_EVENT_ID, str(cursor[LAST_EVENT_ID]))
            sync.set_state(LAST_AUDIT_ID, str(cursor[LAST_AUDIT_ID]))

            for item in package["module_qr"]:
                row = registry.get(item["module_qr"])
                if row is None:
                    continue
                owner = conflicted.get(row.module_qr)
                if owner is not None:
                    if row.server_status == "CONFLICT":
                        continue
                    event = self._mark_conflict(row, owner)
                    new_conflicts += 1
                    if event is not None:
                        uow.events.append(event)
                elif row.server_status != "CONFLICT":
                    row.server_status = "SYNCED"
            uow.commit()
        return new_conflicts

    def _finish_run(self, run_id: int, status: str, totals: dict, detail: str, *, failed_outbox: list[int] | None = None) -> None:
        with UnitOfWork(self._session_factory) as uow:
            sync = SyncRepository(uow.session)
            from stationapp.data.models import SyncRunModel

            run = uow.session.get(SyncRunModel, run_id)
            run.status = status
            run.finished_at = _utc_now()
            run.detail = detail[:2000]
            for key, value in totals.items():
                setattr(run, key, value)
            if status == "FAILED":
                # Every waiting batch records the failed attempt, even if
                # the outage happened before its package was built.
                pending = [row.id for row in sync.pending_outbox(100_000)]
                sync.mark_failed(pending, detail, _utc_now() + timedelta(minutes=5))
            uow.commit()


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class SyncScheduler:
    """Runs syncs on a background thread; retries while still due."""

    def __init__(self, service: ServerSyncService, *, retry_seconds: float = 300.0) -> None:
        self._service = service
        self._retry_seconds = retry_seconds
        self._listeners: list[Callable[[], None]] = []
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_report: SyncReport | None = None

    @property
    def service(self) -> ServerSyncService:
        return self._service

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def on_batch_committed(self, _batch=None) -> None:
        """Production hook: start a sync once the module threshold is met."""
        if self._service.configured and self._service.threshold_reached():
            self.request("THRESHOLD")
        else:
            # Pending count changed; keep indicators current.
            self._notify()

    def request(self, trigger: str) -> bool:
        """Start a background sync; False if one is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel_timer()
            self._thread = threading.Thread(target=self._run, args=(trigger,), name="lan-sync", daemon=True)
            self._thread.start()
        self._notify()
        return True

    def sync_now(self, trigger: str) -> SyncReport:
        """Blocking sync (e.g. before closing the session)."""
        self.wait(timeout=120.0)
        try:
            report = self._service.sync(trigger)
        except SyncInProgress as exc:
            report = SyncReport(trigger, False, detail=str(exc))
        self.last_report = report
        self._notify()
        return report

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_timer()

    def _run(self, trigger: str) -> None:
        try:
            report = self._service.sync(trigger)
        except SyncInProgress:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Background sync failed")
            report = SyncReport(trigger, False, detail="Unexpected error")
        self.last_report = report
        if not report.success and self._service.configured and self._service.threshold_reached():
            with self._lock:
                self._cancel_timer()
                self._timer = threading.Timer(self._retry_seconds, lambda: self.request("RETRY"))
                self._timer.daemon = True
                self._timer.start()
        self._notify()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                logger.exception("Sync listener failed")
