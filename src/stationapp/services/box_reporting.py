"""Reporting finished trays to the box, and the station's hold.

A committed batch (one tray) waits in the outbox, saved in the same
transaction as the commit, until the box has recorded it (R1, R11, R12).
Each report carries, for every module that received a box address, its
QR, that address, whether it passed and its Batch Code (the SDK 7.0
batch_id, given when the tray was committed): the Stock MAC in Stock
Only mode, the Pricol MAC in Stock + Pricol mode. Temporary Stock MACs and
modules that never received a box address are not reported.

Reports run on a background thread and never stop the line (R13). While
the box is unreachable, busy, or the operator's box session has ended,
the trays stay queued and go out together later.

A report answering on_hold stops new trays until a supervisor clears
the hold at the station with their phone and PIN (R14-R16). The hold
and the rejected modules are saved, so they survive a restart.

The station also holds itself when a QR already bound here is scanned
again (a re-worked module: same QR, new MAC). Scanning stops until a
supervisor clears the hold; clearing it lets that QR be bound once more,
and the box then records the module as re-programmed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from unisemble_client import InvalidBatchIdError, InvalidMacError, Module, errors

from stationapp.data.models import StationSettingModel
from stationapp.data.sync_repository import QrRegistryRepository, ReportQueueRepository
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.domain.events import EventType, ManufacturingEvent
from stationapp.domain.mac import MacAddress
from stationapp.domain.users import AuthenticationFailed, AuthorizationError
from stationapp.drivers.box.gateway import BoxGateway
from stationapp.services.box_blocks import BoxBlockService, describe_box_error

logger = logging.getLogger(__name__)

HOLD_KEY = "box_hold"
QR_REUSE_KEY = "qr_reuse_hold"
MAX_MODULES_PER_REPORT = 500
NOTE_LIMIT = 500

REJECT_WORDS = {
    "duplicate": "This address is already on another module. Two modules must never share an address. "
                 "Take this module out and keep it apart.",
    "not_this_station": "This address was never given to this station. Take this module out and keep it apart.",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReportInProgress(Exception):
    """A report is already running."""


@dataclass(frozen=True, slots=True)
class RejectedModule:
    qr: str
    mac: str
    reason: str

    @property
    def words(self) -> str:
        return REJECT_WORDS.get(self.reason, f"Rejected by the box: {self.reason}.")


@dataclass(frozen=True, slots=True)
class ReusedQr:
    """A QR scanned again that is already bound on this station."""

    qr: str
    previous: str       # where it is bound, e.g. "this station, batch B-1, slot 2"
    batch_id: str       # the tray in which it was scanned again
    since: str


@dataclass(frozen=True, slots=True)
class HoldState:
    """The line is stopped: on hold by the box, or by a re-used QR."""

    on_hold: bool = False
    reason: str | None = None
    rejected: tuple[RejectedModule, ...] = ()
    since: str | None = None
    box_on_hold: bool = False
    reused_qrs: tuple[ReusedQr, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportRun:
    trigger: str
    success: bool
    modules: int = 0
    batches: int = 0
    rejected: int = 0
    detail: str = ""
    login_required: bool = False


@dataclass(frozen=True, slots=True)
class ReportStatus:
    configured: bool
    running: bool
    pending_modules: int
    pending_batches: int
    hold: HoldState
    login_required: bool
    last_success_at: datetime | None
    last_error: str | None

    @property
    def on_hold(self) -> bool:
        return self.hold.on_hold


@dataclass(slots=True)
class _Pending:
    rows: list = field(default_factory=list)
    modules: list[Module] = field(default_factory=list)
    where: dict[int, tuple[str, str, int]] = field(default_factory=dict)   # module index -> (batch, jig, slot)


def modules_of(record: dict[str, Any]) -> list[tuple[int, Module]]:
    """(slot, Module) for every module of a committed batch record that
    received a box address."""
    final_mac = "pricol_mac" if record.get("programming_mode") == "STOCK_AND_PRICOL" else "stock_mac"
    modules = []
    for slot in record.get("slots", []):
        mac, qr = slot.get(final_mac), slot.get("module_qr")
        if not mac or not qr or slot.get("disposition") == "EMPTY":
            continue
        passed = slot.get("disposition") == "PASS"
        note = None if passed else (slot.get("hold_reason") or slot.get("disposition") or "failed")[:NOTE_LIMIT]
        modules.append((slot["slot_number"], Module(qr=qr, mac=MacAddress.parse(mac).compact, passed=passed, note=note, batch_id=slot.get("batch_code"))))
    return modules


class BoxReportService:
    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, gateway: BoxGateway | None, blocks: BoxBlockService | None = None, on_session_ended: Callable[[], None] | None = None) -> None:
        self._session_factory = session_factory
        self._station_id = station_id
        self._gateway = gateway
        self._blocks = blocks
        self._on_session_ended = on_session_ended
        self._lock = threading.Lock()
        self._running = False
        self._login_required = False
        self._last_error: str | None = None
        self._hold_listeners: list[Callable[[], None]] = []

    @property
    def configured(self) -> bool:
        return self._gateway is not None

    @property
    def gateway(self) -> BoxGateway | None:
        return self._gateway

    @property
    def running(self) -> bool:
        return self._running

    @property
    def login_required(self) -> bool:
        """Modules wait because no one holds a box session."""
        return self._login_required

    def set_session_listener(self, callback: Callable[[], None]) -> None:
        self._on_session_ended = callback

    def add_hold_listener(self, callback: Callable[[], None]) -> None:
        """callback() after the station put itself on hold."""
        self._hold_listeners.append(callback)

    # ------------------------------------------------------------------

    def report(self, trigger: str) -> ReportRun:
        """Send every queued module, up to one report of 500 modules."""
        if not self._lock.acquire(blocking=False):
            raise ReportInProgress("A report is already running")
        self._running = True
        try:
            return self._report(trigger)
        finally:
            self._running = False
            self._lock.release()

    def _report(self, trigger: str) -> ReportRun:
        if self._gateway is None:
            return ReportRun(trigger, False, detail="Box not configured")
        if self._blocks is not None:
            self._blocks.send_owed_promotes()

        pending = self._collect()
        if pending.rows and not pending.modules:
            # Trays in which no module received a box address.
            self._record(trigger, pending, None)
            return ReportRun(trigger, True, batches=len(pending.rows), detail="Nothing to report to the box")
        if not pending.modules:
            return ReportRun(trigger, True, detail="Nothing waiting")
        if self._gateway.holder is None:
            self._login_required = True
            return ReportRun(trigger, False, modules=len(pending.modules), detail="Waiting for an operator login", login_required=True)

        try:
            result = self._gateway.report(pending.modules)
        except (errors.SessionExpired, errors.NotLoggedIn):
            self._login_required = True
            self._failed(trigger, pending, "The operator's box session has ended - log in again")
            if self._on_session_ended is not None:
                self._on_session_ended()
            return ReportRun(trigger, False, modules=len(pending.modules), detail="Box session ended; log in again", login_required=True)
        except InvalidBatchIdError as exc:
            # Only a tray committed before Batch Codes (SDK 7.0) lacks one.
            detail = f"{exc}. A tray finished before Batch Codes were introduced cannot be reported to a 7.0 box; call the Supervisor."
            self._failed(trigger, pending, detail)
            return ReportRun(trigger, False, modules=len(pending.modules), detail=detail)
        except (errors.UnisembleError, InvalidMacError) as exc:
            detail = describe_box_error(exc) if isinstance(exc, errors.UnisembleError) else str(exc)
            self._failed(trigger, pending, detail)
            return ReportRun(trigger, False, modules=len(pending.modules), detail=detail)

        self._login_required = False
        self._last_error = None
        self._record(trigger, pending, result)
        logger.info(
            "Box report | modules=%d accepted=%d reprogrammed=%d rejected=%d on_hold=%s",
            len(pending.modules), len(result.accepted), len(result.reprogrammed), len(result.rejected), result.on_hold,
        )
        if self._blocks is not None:
            self._blocks.top_up_in_use()
        return ReportRun(trigger, True, modules=len(pending.modules), batches=len(pending.rows), rejected=len(result.rejected))

    def _collect(self) -> _Pending:
        pending = _Pending()
        with self._session_factory() as session:
            for row in ReportQueueRepository(session).pending():
                modules = modules_of(row.payload)
                if pending.modules and len(pending.modules) + len(modules) > MAX_MODULES_PER_REPORT:
                    break
                pending.rows.append(row)
                for slot, module in modules:
                    pending.where[len(pending.modules)] = (row.aggregate_id, row.payload.get("jig_id") or "?", slot)
                    pending.modules.append(module)
            session.expunge_all()
        return pending

    def _record(self, trigger: str, pending: _Pending, result) -> None:
        with UnitOfWork(self._session_factory) as uow:
            queue = ReportQueueRepository(uow.session)
            registry = QrRegistryRepository(uow.session)
            run = queue.start_run(trigger)
            rows = [uow.session.merge(row) for row in pending.rows]
            queue.mark_uploaded(rows)
            rejected: list[RejectedModule] = []
            if result is not None:
                # The SDK returns the very Module objects that were sent.
                reasons = {id(module): reason for module, reason in result.rejected}
                for index, module in enumerate(pending.modules):
                    reason = reasons.get(id(module))
                    if reason is None:
                        registry.set_status(module.qr, "SYNCED")
                        continue
                    item = RejectedModule(module.qr, module.mac, reason)
                    rejected.append(item)
                    registry.set_status(module.qr, "CONFLICT", item.words)
                    batch_id, jig_id, slot = pending.where[index]
                    uow.events.append(self._event(batch_id, jig_id, slot, module, EventType.QR_CONFLICT_REPORTED, reason=reason, words=item.words))
                for row in rows:
                    uow.events.append(ManufacturingEvent(
                        event_type=EventType.UPLOAD_CONFIRMED,
                        batch_id=row.aggregate_id,
                        station_id=self._station_id,
                        jig_id=row.payload.get("jig_id") or "?",
                        payload={"reported_to": "box", "trigger": trigger},
                    ))
                self._save_hold(uow.session, result.on_hold, result.hold_reason, rejected)
            run.status = "SUCCESS"
            run.finished_at = _utc_now()
            run.modules = len(pending.modules)
            run.batches = len(rows)
            run.qr_conflicts = len(rejected)
            run.detail = None if result is None else f"accepted {len(result.accepted)}, reprogrammed {len(result.reprogrammed)}, rejected {len(result.rejected)}"
            uow.commit()

    def _failed(self, trigger: str, pending: _Pending, detail: str) -> None:
        self._last_error = detail
        logger.warning("Box report not sent: %s", detail)
        with UnitOfWork(self._session_factory) as uow:
            queue = ReportQueueRepository(uow.session)
            run = queue.start_run(trigger)
            queue.mark_failed([row.id for row in pending.rows], detail)
            run.status = "FAILED"
            run.finished_at = _utc_now()
            run.modules = len(pending.modules)
            run.batches = len(pending.rows)
            run.detail = detail[:2000]
            uow.commit()

    def _event(self, batch_id: str, jig_id: str, slot: int, module: Module, event_type: EventType, **payload) -> ManufacturingEvent:
        return ManufacturingEvent(
            event_type=event_type,
            batch_id=batch_id,
            station_id=self._station_id,
            jig_id=jig_id,
            slot_number=slot,
            module_qr=module.qr,
            payload={"mac": module.mac, **payload},
        )

    # -- hold ------------------------------------------------------------

    def hold(self) -> HoldState:
        with self._session_factory() as session:
            row = session.get(StationSettingModel, HOLD_KEY)
            box = row.value if row is not None and row.value.get("on_hold") else {}
            reused = tuple(ReusedQr(**item) for item in _qr_reuse_value(session)["held"])
        if not box and not reused:
            return HoldState()
        reasons = [box["reason"]] if box.get("reason") else []
        reasons += [f"QR {item.qr} scanned again; already bound at {item.previous}" for item in reused]
        return HoldState(
            on_hold=True,
            reason="; ".join(reasons) or "On hold by the box",
            rejected=tuple(RejectedModule(**item) for item in box.get("rejected", [])),
            since=box.get("since") or (reused[0].since if reused else None),
            box_on_hold=bool(box),
            reused_qrs=reused,
        )

    # -- QR scanned again (same QR, new MAC) -------------------------------

    def hold_for_reused_qr(self, qr: str, previous: str, batch_id: str) -> None:
        """Stop the line: qr is already bound on this station."""
        with UnitOfWork(self._session_factory) as uow:
            value = _qr_reuse_value(uow.session)
            if not any(item["qr"] == qr for item in value["held"]):
                value["held"].append({"qr": qr, "previous": previous, "batch_id": batch_id, "since": _utc_now().isoformat()})
            _save_qr_reuse(uow.session, value, "STATION")
            uow.audit.append(AuditRecord(
                user_id="SYSTEM",
                station_id=self._station_id,
                record_type="QR_REUSE_HOLD",
                record_id=qr,
                action="HOLD_SET",
                reason=f"QR {qr} scanned again in batch {batch_id}; already bound at {previous}",
                new_value={"qr": qr, "previous": previous, "batch_id": batch_id},
            ))
            uow.commit()
        logger.warning("Station on hold: QR %s scanned again in batch %s (already bound at %s)", qr, batch_id, previous)
        for callback in tuple(self._hold_listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - an indicator must not stop the hold
                logger.exception("Hold listener failed")

    def scanning_stopped(self) -> bool:
        """True while a re-used QR waits for a supervisor."""
        with self._session_factory() as session:
            return bool(_qr_reuse_value(session)["held"])

    def reuse_approved(self, qr: str) -> bool:
        with self._session_factory() as session:
            return qr in _qr_reuse_value(session)["approved"]

    def reuse_bound(self, qr: str) -> None:
        """The approved QR is bound again; the approval is used up."""
        with UnitOfWork(self._session_factory) as uow:
            value = _qr_reuse_value(uow.session)
            if qr in value["approved"]:
                value["approved"].remove(qr)
                _save_qr_reuse(uow.session, value, "STATION")
                uow.commit()

    def clear_hold(self, phone: str, pin: str) -> None:
        """A supervisor's phone and PIN, typed at the station (R16).

        Raises AuthenticationFailed or AuthorizationError when the hold
        stays; the line restarts only when this returns.
        """
        if self._gateway is None:
            raise AuthenticationFailed("The box is not configured")
        try:
            cleared = self._gateway.clear_hold(phone.strip(), pin)
        except errors.WrongPin as exc:
            raise AuthenticationFailed("Phone number or PIN not recognised.") from exc
        except errors.AccountLocked as exc:
            raise AuthenticationFailed(f"Too many wrong PINs. Try again in {exc.retry_after_seconds // 60 + 1} minutes.") from exc
        except errors.UnisembleError as exc:
            raise AuthenticationFailed(f"Hold not cleared: {describe_box_error(exc)}.") from exc
        if not cleared:
            raise AuthorizationError("This person cannot clear holds: not a supervisor, or they must set a new PIN first.")

        previous = self.hold()
        with UnitOfWork(self._session_factory) as uow:
            self._save_hold(uow.session, False, None, [])
            reuse = _qr_reuse_value(uow.session)
            if reuse["held"]:
                # The supervisor has seen the module: its QR may be bound again.
                reuse["approved"] += [item["qr"] for item in reuse["held"] if item["qr"] not in reuse["approved"]]
                reuse["held"] = []
                _save_qr_reuse(uow.session, reuse, phone.strip() or "SUPERVISOR")
            uow.audit.append(AuditRecord(
                user_id=phone.strip() or "UNKNOWN",
                station_id=self._station_id,
                record_type="BOX_HOLD",
                record_id=self._station_id,
                action="HOLD_CLEARED",
                reason="Hold cleared with the supervisor's phone and PIN",
                previous_value={
                    "reason": previous.reason,
                    "rejected": [_rejected_json(item) for item in previous.rejected],
                    "reused_qrs": [item.qr for item in previous.reused_qrs],
                },
                new_value={"on_hold": False},
            ))
            uow.commit()
        logger.info("Station hold cleared by a supervisor")

    def _save_hold(self, session: Session, on_hold: bool, reason: str | None, rejected: list[RejectedModule]) -> None:
        row = session.get(StationSettingModel, HOLD_KEY)
        if not on_hold:
            value: dict[str, Any] = {"on_hold": False}
        else:
            previous = row.value if row is not None and row.value.get("on_hold") else {}
            value = {
                "on_hold": True,
                "reason": reason or previous.get("reason"),
                "rejected": list(previous.get("rejected", [])) + [_rejected_json(item) for item in rejected],
                "since": previous.get("since") or _utc_now().isoformat(),
            }
        if row is None:
            session.add(StationSettingModel(key=HOLD_KEY, value=value, updated_at=_utc_now(), updated_by="BOX"))
        else:
            row.value = value
            row.updated_at = _utc_now()
            row.updated_by = "BOX"
        session.flush()

    # -- status ----------------------------------------------------------

    def status(self) -> ReportStatus:
        with self._session_factory() as session:
            queue = ReportQueueRepository(session)
            rows = queue.pending()
            modules = sum(len(modules_of(row.payload)) for row in rows)
            last = queue.last_success()
            last_success_at = last.finished_at if last is not None else None
        return ReportStatus(
            configured=self.configured,
            running=self._running,
            pending_modules=modules,
            pending_batches=len(rows),
            hold=self.hold(),
            login_required=self._login_required,
            last_success_at=last_success_at,
            last_error=self._last_error,
        )


def _rejected_json(item: RejectedModule) -> dict[str, str]:
    return {"qr": item.qr, "mac": item.mac, "reason": item.reason}


def _qr_reuse_value(session: Session) -> dict[str, list]:
    row = session.get(StationSettingModel, QR_REUSE_KEY)
    value = dict(row.value) if row is not None else {}
    return {"held": list(value.get("held", [])), "approved": list(value.get("approved", []))}


def _save_qr_reuse(session: Session, value: dict[str, list], updated_by: str) -> None:
    row = session.get(StationSettingModel, QR_REUSE_KEY)
    if row is None:
        session.add(StationSettingModel(key=QR_REUSE_KEY, value=value, updated_at=_utc_now(), updated_by=updated_by))
    else:
        row.value, row.updated_at, row.updated_by = value, _utc_now(), updated_by
    session.flush()


class BoxReportScheduler:
    """Runs reports on a background thread; retries while modules wait."""

    def __init__(self, service: BoxReportService, *, retry_seconds: float = 60.0) -> None:
        self._service = service
        self._retry_seconds = retry_seconds
        self._listeners: list[Callable[[], None]] = []
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_report: ReportRun | None = None
        service.add_hold_listener(self._notify)

    @property
    def service(self) -> BoxReportService:
        return self._service

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def on_batch_committed(self, _batch=None) -> None:
        """Production hook: report each finished tray once (R11)."""
        self.request("TRAY")

    def request(self, trigger: str) -> bool:
        """Start a background report; False if one is already running."""
        if not self._service.configured:
            self._notify()
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel_timer()
            self._thread = threading.Thread(target=self._run, args=(trigger,), name="box-report", daemon=True)
            self._thread.start()
        self._notify()
        return True

    def report_now(self, trigger: str) -> ReportRun:
        """Blocking report (e.g. before logout or closing the session)."""
        self.wait(timeout=60.0)
        try:
            run = self._service.report(trigger)
        except ReportInProgress as exc:
            run = ReportRun(trigger, False, detail=str(exc))
        self.last_report = run
        self._notify()
        return run

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_timer()

    def _run(self, trigger: str) -> None:
        try:
            run = self._service.report(trigger)
        except ReportInProgress:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Background box report failed")
            run = ReportRun(trigger, False, detail="Unexpected error")
        self.last_report = run
        if not run.success and not run.login_required:
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
            except Exception:  # noqa: BLE001 - an indicator must not break reporting
                logger.exception("Report listener failed")
