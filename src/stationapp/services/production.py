"""Production orchestrator: drives one batch through the fixed workflow.

This is the application state/service layer the README requires to
enforce the production sequence. The GUI only asks it to start a batch,
advance, scan a QR or release a finished batch; every rule about what
may happen next lives in the Batch aggregate and in this controller,
never in button enablement.

Threading model
---------------
advance() blocks for the duration of an automatic stage and is meant to
run on an OperationManager worker thread. Inside a stage, every eligible
slot gets its own worker (one MPCLI subprocess per populated slot).
All Batch mutations happen under a single lock, so the GUI can take a
consistent snapshot() at any time from the Qt thread.

Persistence model
-----------------
State is written before and after every external operation. In
particular, MACs are persisted as PROGRAMMING before MPCLI starts, so a
crash leaves them for RecoveryRepository to quarantine rather than
silently returning them to the pool.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from stationapp.concurrency.cancellation import CancellationToken
from stationapp.domain.batch import Batch, BatchState, ProgrammingMode, SlotDisposition
from stationapp.domain.errors import DomainError, DuplicateModuleQr, VerificationMismatch
from stationapp.domain.events import EventType, ManufacturingEvent
from stationapp.domain.golden_rig import GoldenRigTarget
from stationapp.domain.mac import AllocatedMac, MacPurpose
from stationapp.domain.slot import DeviceState
from stationapp.domain.users import AccessError, AuthenticatedUser
from stationapp.drivers.mpcli.driver import PHASE_FLASHING, PHASE_WRITING_MAC
from stationapp.drivers.mpcli.types import IdentityOutcome
from stationapp.drivers.pricol.functional import FunctionalTestOutcome
from stationapp.services.qr_registry import DuplicateQrError, ModuleQrClaimService
from stationapp.services.golden_rig_eligibility import GoldenRigEligibilityService
from stationapp.services.golden_rig_result_processor import GoldenRigResultProcessor
from stationapp.services.golden_rig_service import GoldenRigTestReport
from stationapp.services.jig_status_projection import JigStatusProjection, JigView
from stationapp.services.programming import (
    ProgrammingTarget,
    SlotProgrammingResult,
    SlotReadbackResult,
)
from stationapp.services.workflow import OperatorWorkflow, WorkflowService

logger = logging.getLogger(__name__)

# Printable ASCII without spaces. Tighten once the module QR format is fixed.
DEFAULT_QR_PATTERN = r"^[!-~]{4,128}$"

ProgrammerFactory = Callable[[Path | None, Path | None], "SlotProgrammer"]


class ProductionError(Exception):
    """A production command was refused; the message is operator-facing."""


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class SlotProgrammer(Protocol):
    """ProgrammingService, or a simulator with the same surface."""

    def program_stock(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None) -> SlotProgrammingResult: ...

    def program_pricol(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None) -> SlotProgrammingResult: ...

    def verify_stock_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> SlotReadbackResult: ...

    def verify_pricol_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> SlotReadbackResult: ...


class RfTester(Protocol):
    """GoldenRigService, or a simulator with the same surface."""

    def run_test(self, *, batch_id: str, station_id: str, jig_id: str, targets: tuple[GoldenRigTarget, ...], cancel_event: threading.Event | None = None) -> GoldenRigTestReport: ...


class FunctionalTester(Protocol):
    """Pricol UART functional test (PRICOLAPP / PRICOLDFU after reset).

    Returns True (pass), False (fail) or None when the outcome could not
    be established, or a FunctionalTestOutcome carrying that verdict plus
    the command/response evidence. None quarantines the slot on HOLD.
    """

    def run_app_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> bool | None | FunctionalTestOutcome: ...

    def run_dfu_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> bool | None | FunctionalTestOutcome: ...


class ProductionAccess(Protocol):
    """StationAccessService: who may run production right now."""

    def require_production_access(self) -> tuple[str, str]:
        """(session_id, operator_id), or raise AccessError."""
        ...


class ProductionStore(Protocol):
    """SqlProductionStore, or an in-memory double in tests."""

    def create(self, batch: Batch, events: Iterable[ManufacturingEvent] = ()) -> None: ...

    def reserve_macs(self, batch: Batch, purpose: MacPurpose, slot_numbers: tuple[int, ...]) -> tuple[AllocatedMac, ...]: ...

    def save(self, batch: Batch, events: Iterable[ManufacturingEvent] = ()) -> None: ...

    def save_and_enqueue(self, batch: Batch, events: Iterable[ManufacturingEvent], upload_payload: dict[str, Any]) -> str: ...

    def module_qr_in_use(self, module_qr: str) -> bool: ...


# ---------------------------------------------------------------------------
# Values exchanged with the GUI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotPort:
    """Session binding of one physical slot to its programming interface."""

    slot_number: int
    port_identity: str
    com_port: str


@dataclass(frozen=True, slots=True)
class SlotActivity:
    text: str
    progress: int | None = None


@dataclass(frozen=True, slots=True)
class ProductionSnapshot:
    """Consistent, immutable view of the controller for rendering."""

    workflow: OperatorWorkflow | None
    jig: JigView | None
    last_error: str | None
    running: bool
    programming_mode: ProgrammingMode | None = None
    batch_label: str | None = None
    occupied_slots: tuple[int, ...] = ()

    @property
    def has_batch(self) -> bool:
        return self.workflow is not None


# States whose primary action is an operator confirmation followed,
# possibly, by an automatic stage.
_OPERATOR_STATES = frozenset({
    BatchState.STOCK_MACS_RESERVED,
    BatchState.AWAITING_STOCK_PROGRAM_MODE,
    BatchState.STOCK_READBACK_VERIFIED,
    BatchState.AWAITING_STOCK_RF_MODE,
    BatchState.PRICOL_MACS_RESERVED,
    BatchState.AWAITING_PRICOL_PROGRAM_MODE,
    BatchState.PRICOL_READBACK_VERIFIED,
    BatchState.AWAITING_FUNCTIONAL_TEST_MODE,
    BatchState.AWAITING_PRICOLAPP_RESET,
    BatchState.AWAITING_PRICOLDFU_RESET,
    BatchState.QR_BOUND,
})

_FINISHED_STATES = frozenset({
    BatchState.COMMITTED,
    BatchState.UPLOAD_PENDING,
    BatchState.UPLOADED,
    BatchState.ABORTED,
})


class ProductionController:
    """Owns the active batch and executes the production workflow."""

    def __init__(self, *, station_id: str, jig_id: str, slot_count: int, store: ProductionStore, rf_tester: RfTester, functional_tester: FunctionalTester, programmer: SlotProgrammer | None = None, programmer_factory: ProgrammerFactory | None = None, readiness_gate: Callable[[ProgrammingMode, tuple[int, ...]], tuple[str, ...]] | None = None, qr_pattern: str = DEFAULT_QR_PATTERN, require_firmware: bool = True, access: ProductionAccess | None = None, qr_claims: ModuleQrClaimService | None = None) -> None:
        """programmer_factory builds a programmer for the firmware images
        chosen at batch setup; programmer is a fixed alternative."""
        if slot_count not in (4, 8):
            raise ValueError("slot_count must be 4 or 8")
        if (programmer is None) == (programmer_factory is None):
            raise ValueError("Provide exactly one of programmer or programmer_factory")

        self._station_id = station_id
        self._jig_id = jig_id
        self._slot_count = slot_count
        self._store = store
        self._programmer = programmer
        self._programmer_factory = programmer_factory
        self._require_firmware = require_firmware
        self._access = access
        self._qr_claims = qr_claims
        self._commit_listeners: list[Callable[[Batch], None]] = []
        self._rf_tester = rf_tester
        self._functional = functional_tester
        self._readiness_gate = readiness_gate
        self._qr_pattern = re.compile(qr_pattern)

        self._lock = threading.RLock()
        self._batch: Batch | None = None
        self._ports: dict[int, SlotPort] = {}
        self._activity: dict[int, SlotActivity] = {}
        self._last_error: str | None = None
        self._running = False
        self._listeners: list[Callable[[], None]] = []

        self._workflow = WorkflowService()
        self._projection = JigStatusProjection()
        self._rf_eligibility = GoldenRigEligibilityService()
        self._rf_processor = GoldenRigResultProcessor()

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        """callback is invoked from any thread after each state change."""
        self._listeners.append(callback)

    @property
    def batch(self) -> Batch | None:
        return self._batch

    @property
    def has_active_batch(self) -> bool:
        """A batch exists and is not yet committed or aborted."""
        with self._lock:
            return self._batch is not None and self._batch.state not in _FINISHED_STATES

    @property
    def slot_count(self) -> int:
        return self._slot_count

    def snapshot(self) -> ProductionSnapshot:
        with self._lock:
            batch = self._batch
            if batch is None:
                return ProductionSnapshot(workflow=None, jig=None, last_error=self._last_error, running=self._running)

            view = self._projection.project(batch)
            if self._activity:
                view = replace(
                    view,
                    slots=tuple(
                        self._overlay(slot_view)
                        for slot_view in view.slots
                    ),
                )

            return ProductionSnapshot(
                workflow=self._workflow.describe(batch),
                jig=view,
                last_error=self._last_error,
                running=self._running,
                programming_mode=batch.programming_mode,
                batch_label=batch.batch_label,
                occupied_slots=tuple(batch.occupied_slot_numbers or ()),
            )

    def _overlay(self, slot_view):
        activity = self._activity.get(slot_view.number)
        if activity is None:
            return slot_view
        return replace(
            slot_view,
            stage=activity.text,
            progress=activity.progress if activity.progress is not None else slot_view.progress,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def start_batch(self, *, occupied: tuple[int, ...], ports: Mapping[int, SlotPort], mode: ProgrammingMode, batch_label: str | None = None, operator_id: str | None = None, stock_firmware: Path | None = None, pricol_firmware: Path | None = None) -> Batch:
        """READY -> START: create the batch, bind ports, reserve Stock MACs."""
        with self._lock:
            if self._running:
                raise ProductionError("Another production step is still running")

            if self._batch is not None and self._batch.state not in _FINISHED_STATES:
                raise ProductionError("The current batch has not been completed")

            session_id = None
            if self._access is not None:
                # The authenticated Operator, never a typed-in name, owns the batch.
                session_id, operator_id = self._require_access()

            occupied = tuple(sorted(occupied))
            if not occupied:
                raise ProductionError("Select at least one loaded slot")

            missing = [number for number in occupied if number not in ports]
            if missing:
                raise ProductionError(
                    "No verified programming interface for slot(s) "
                    + ", ".join(str(number) for number in missing)
                )

            if self._readiness_gate is not None:
                problems = self._readiness_gate(mode, occupied)
                if problems:
                    raise ProductionError("Station is not READY: " + "; ".join(problems))

            if mode is ProgrammingMode.STOCK_ONLY:
                pricol_firmware = None

            firmware = {}
            if self._programmer_factory is not None:
                if self._require_firmware and stock_firmware is None:
                    raise ProductionError("Select the Stock firmware image")
                if self._require_firmware and mode is ProgrammingMode.STOCK_AND_PRICOL and pricol_firmware is None:
                    raise ProductionError("Select the Pricol firmware image")
                try:
                    self._programmer = self._programmer_factory(stock_firmware, pricol_firmware)
                    firmware = {
                        "stock_firmware": _firmware_identity(stock_firmware),
                        "pricol_firmware": _firmware_identity(pricol_firmware),
                    }
                except (OSError, ValueError) as exc:
                    raise ProductionError(f"Firmware configuration invalid: {exc}") from exc

            batch = Batch(
                station_id=self._station_id,
                jig_id=self._jig_id,
                slot_count=self._slot_count,
                programming_mode=mode,
                batch_label=(batch_label or "").strip() or None,
                operator_id=(operator_id or "").strip() or None,
                session_id=session_id,
            )
            batch.configure_occupied_slots(occupied)

            self._store.create(batch, [self._event(batch, EventType.BATCH_CREATED, mode=mode.value, occupied=list(occupied), batch_label=batch.batch_label, operator_id=batch.operator_id, session_id=session_id, **firmware)])

            self._batch = batch
            self._ports = {number: ports[number] for number in occupied}
            self._activity = {}
            self._last_error = None

            batch.begin_port_binding()
            events = []
            for number in occupied:
                port = self._ports[number]
                batch.bind_port(number, port.port_identity)
                events.append(self._event(batch, EventType.PORT_BOUND, slot=number, port_identity=port.port_identity, com_port=port.com_port))
            self._store.save(batch, events)

            try:
                records = self._store.reserve_macs(batch, MacPurpose.STOCK_RF_TEST, occupied)
            except Exception as exc:
                # Reservation is atomic in the repository: nothing was consumed.
                batch.abort(f"Stock MAC reservation failed: {exc}")
                self._store.save(batch, [self._event(batch, EventType.HOLD_PLACED, reason=batch.hold_reason)])
                self._notify()
                raise ProductionError(str(batch.hold_reason)) from exc

            batch.accept_stock_reservations(records)
            self._store.save(batch, [
                self._event(batch, EventType.STOCK_MAC_RESERVED, slot=record.slot_number, mac=str(record.address), allocation_id=record.allocation_id)
                for record in records
            ])

            logger.info("Batch started | batch_id=%s mode=%s occupied=%s", batch.batch_id, mode.value, occupied)

        self._notify()
        return batch

    def advance(self, cancellation_token: CancellationToken | None = None) -> None:
        """Perform the current operator confirmation and any automatic stage.

        Runs until the batch next needs the operator. Blocking: call on a
        worker thread.
        """
        with self._lock:
            batch = self._require_batch()
            if self._running:
                raise ProductionError("A production step is already running")
            if self._access is not None:
                try:
                    self._require_access()
                except ProductionError as exc:
                    self._last_error = str(exc)
                    self._notify()
                    raise
            self._running = True
            self._last_error = None

        self._notify()

        try:
            self._advance(batch, cancellation_token)
        except ProductionError as exc:
            self._fail(str(exc))
            raise
        except DomainError as exc:
            self._fail(str(exc))
            raise ProductionError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Production step failed | batch_id=%s", batch.batch_id)
            self._fail(f"Unexpected error: {exc}")
            raise
        finally:
            with self._lock:
                self._running = False
                self._activity = {}
                if self._batch is not None:
                    try:
                        self._store.save(self._batch)
                    except Exception:  # noqa: BLE001 - do not mask the original error
                        logger.exception("Could not persist batch state after step")
            self._notify()

    def scan_qr(self, raw: str) -> int:
        """Bind a scanned QR to the next eligible slot (ascending order)."""
        with self._lock:
            batch = self._require_batch()

            if self._running:
                raise ProductionError("Wait for the current step to finish before scanning")

            if batch.state is not BatchState.QR_SCANNING:
                raise ProductionError("QR scanning is not active")

            if self._access is not None:
                self._require_access()

            qr = raw.strip()
            if not self._qr_pattern.fullmatch(qr):
                raise ProductionError(f"Invalid QR data: {qr!r}")

            owner = self._qr_owner(qr)
            if owner is not None:
                raise ProductionError(f"Duplicate QR: {qr} is already bound at {owner}")

            expected_slot = batch.next_qr_slot
            if expected_slot is None:
                raise ProductionError("All eligible module QRs have been scanned")

            # Claim the QR line-wide before binding it. Offline, the
            # binding proceeds UNCONFIRMED and is checked at the next sync.
            server_status, claim_detail = "UNCONFIRMED", "no LAN claim service"
            if self._qr_claims is not None:
                try:
                    decision = self._qr_claims.claim(qr, batch_id=batch.batch_id, slot_number=expected_slot)
                except DuplicateQrError as exc:
                    raise ProductionError(str(exc)) from exc
                server_status, claim_detail = decision.server_status, decision.detail

            try:
                slot_number = batch.scan_module_qr(qr)
            except (DuplicateModuleQr, DomainError) as exc:
                raise ProductionError(str(exc)) from exc

            events = [self._event(batch, EventType.MODULE_QR_BOUND, slot=slot_number, module_qr=qr, server_status=server_status, claim_detail=claim_detail)]
            bind = getattr(self._store, "bind_module_qr", None)
            if bind is not None:
                bind(batch, events, module_qr=qr, slot_number=slot_number, server_status=server_status)
            else:
                self._store.save(batch, events)
            self._last_error = None

        self._notify()
        return slot_number

    def _qr_owner(self, qr: str) -> str | None:
        lookup = getattr(self._store, "module_qr_owner", None)
        if lookup is not None:
            return lookup(qr)
        return "another module" if self._store.module_qr_in_use(qr) else None

    def add_commit_listener(self, callback: Callable[[Batch], None]) -> None:
        """callback(batch) after a batch is committed and queued for upload."""
        self._commit_listeners.append(callback)

    def abort_batch(self, *, reason: str, actor_id: str | None = None, supervisor: AuthenticatedUser | None = None) -> None:
        """Supervisor disposition of a batch that cannot continue.

        With access control, supervisor must be an authenticated Supervisor.
        """
        if self._access is not None:
            if supervisor is None or not supervisor.is_supervisor:
                raise ProductionError("Supervisor authorization is required to abort a batch")
        if supervisor is not None:
            actor_id = supervisor.user_id

        with self._lock:
            batch = self._require_batch()
            if self._running:
                raise ProductionError("Stop the running step before aborting")
            if not reason.strip():
                raise ProductionError("An abort reason is required")

            try:
                batch.abort(reason)
            except DomainError as exc:
                raise ProductionError(str(exc)) from exc

            events = [self._event(batch, EventType.HOLD_PLACED, reason=reason.strip(), actor_id=actor_id, action="BATCH_ABORTED")]
            events.extend(self._disposition_events(batch))
            self._store.save(batch, events)
            self._last_error = None

        self._notify()

    def release_batch(self) -> None:
        """Prepare for the next batch once the current one is finished."""
        with self._lock:
            batch = self._require_batch()
            if batch.state not in _FINISHED_STATES:
                raise ProductionError("Only a completed or aborted batch can be released")
            self._batch = None
            self._ports = {}
            self._activity = {}
            self._last_error = None

        self._notify()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _advance(self, batch: Batch, token: CancellationToken | None) -> None:
        state = batch.state

        if state in {BatchState.HOLD, BatchState.ABORTED}:
            raise ProductionError(batch.hold_reason or "Batch requires supervisor review")

        if state in _FINISHED_STATES:
            raise ProductionError("Batch is complete; load the next batch")

        if state not in _OPERATOR_STATES and state not in {
            BatchState.STOCK_PROGRAMMING,
            BatchState.STOCK_PROGRAMMED,
            BatchState.STOCK_RF_TESTING,
            BatchState.STOCK_RF_CONFIRMED,
            BatchState.PRICOL_PROGRAMMING,
            BatchState.PRICOL_PROGRAMMED,
            BatchState.FUNCTIONAL_TEST_COMPLETE,
        }:
            raise ProductionError(f"No operator action is valid in state {state.value}")

        # Operator confirmation for the current step.
        with self._lock:
            confirm = {
                BatchState.STOCK_MACS_RESERVED: batch.request_stock_program_mode,
                BatchState.STOCK_READBACK_VERIFIED: batch.request_stock_rf_mode,
                BatchState.PRICOL_MACS_RESERVED: batch.request_pricol_program_mode,
                BatchState.PRICOL_READBACK_VERIFIED: batch.request_functional_test_mode,
                BatchState.AWAITING_FUNCTIONAL_TEST_MODE: batch.confirm_functional_test_mode,
            }.get(state)

            if confirm is not None:
                confirm()
                self._store.save(batch)
                return

            if state is BatchState.AWAITING_STOCK_PROGRAM_MODE:
                # Persist PROGRAMMING before any MPCLI process starts.
                batch.confirm_stock_program_mode()
                self._store.save(batch, [
                    self._event(batch, EventType.STOCK_PROGRAMMING_STARTED, slot=number, mac=str(batch.slots[number].stock_mac))
                    for number in batch.occupied_slot_numbers
                    if batch.slots[number].state is DeviceState.STOCK_PROGRAMMING
                ])

            elif state is BatchState.AWAITING_STOCK_RF_MODE:
                batch.begin_stock_rf_test()
                self._store.save(batch)

            elif state is BatchState.AWAITING_PRICOL_PROGRAM_MODE:
                batch.confirm_pricol_program_mode()
                self._store.save(batch, [
                    self._event(batch, EventType.PRICOL_PROGRAMMING_STARTED, slot=number, mac=str(batch.slots[number].pricol_mac))
                    for number in batch.occupied_slot_numbers
                    if batch.slots[number].state is DeviceState.PRICOL_PROGRAMMING
                ])

            elif state is BatchState.AWAITING_PRICOLAPP_RESET:
                batch.confirm_pricol_app_reset()
                self._store.save(batch)

            elif state is BatchState.AWAITING_PRICOLDFU_RESET:
                batch.confirm_pricol_dfu_reset()
                self._store.save(batch)

            elif state is BatchState.QR_BOUND:
                self._finalise(batch)
                return

        self._notify()
        self._run_automatic(batch, token)

    def _run_automatic(self, batch: Batch, token: CancellationToken | None) -> None:
        """Run automatic stages until the batch needs the operator."""
        while True:
            state = batch.state

            if state is BatchState.STOCK_PROGRAMMING:
                self._program_stage(batch, MacPurpose.STOCK_RF_TEST, token)
            elif state is BatchState.STOCK_PROGRAMMED:
                self._readback_stage(batch, MacPurpose.STOCK_RF_TEST, token)
            elif state is BatchState.STOCK_RF_TESTING:
                self._rf_stage(batch, token)
            elif state is BatchState.STOCK_RF_CONFIRMED:
                self._after_rf(batch)
            elif state is BatchState.PRICOL_PROGRAMMING:
                self._program_stage(batch, MacPurpose.PRICOL_PRODUCTION, token)
            elif state is BatchState.PRICOL_PROGRAMMED:
                self._readback_stage(batch, MacPurpose.PRICOL_PRODUCTION, token)
            elif state is BatchState.PRICOLAPP_TESTING:
                self._functional_stage(batch, dfu=False, token=token)
            elif state is BatchState.PRICOLDFU_TESTING:
                self._functional_stage(batch, dfu=True, token=token)
            elif state is BatchState.FUNCTIONAL_TEST_COMPLETE:
                with self._lock:
                    batch.begin_qr_scanning()
                    self._store.save(batch, [self._event(batch, EventType.QR_SCANNING_STARTED, slots=[slot.number for slot in batch.qr_candidates])])
            else:
                return

            self._notify()

            if token is not None and token.is_cancelled:
                return

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _program_stage(self, batch: Batch, purpose: MacPurpose, token: CancellationToken | None) -> None:
        stock = purpose is MacPurpose.STOCK_RF_TEST
        busy_state = DeviceState.STOCK_PROGRAMMING if stock else DeviceState.PRICOL_PROGRAMMING

        with self._lock:
            slots = tuple(slot.number for slot in batch.occupied_slots if slot.state is busy_state)
            targets = {number: self._target(batch, number, purpose) for number in slots}

        programmer = self._programmer
        assert programmer is not None

        def run(number: int) -> None:
            target = targets[number]
            self._set_activity(number, "Programming firmware", None)

            def on_phase(phase: str) -> None:
                if phase == PHASE_FLASHING:
                    self._set_activity(number, "Programming firmware", None)
                elif phase == PHASE_WRITING_MAC:
                    self._set_activity(number, "Writing MAC", None)

            try:
                program = programmer.program_stock if stock else programmer.program_pricol
                result = program(target, cancellation_token=token, on_phase=on_phase)
                succeeded = result.programming.succeeded
                detail = {
                    "outcome": result.programming.outcome.value,
                    "message": result.programming.message,
                    "duration_seconds": round(result.programming.process.duration_seconds, 3),
                }
            except Exception as exc:  # noqa: BLE001 - per-slot isolation
                logger.exception("Programming raised | slot=%s", number)
                succeeded = False
                detail = {"outcome": "UNCERTAIN", "message": f"{type(exc).__name__}: {exc}"}

            with self._lock:
                if stock:
                    batch.record_stock_programming(number, succeeded=succeeded)
                else:
                    batch.record_pricol_programming(number, succeeded=succeeded)

                event_type = (
                    (EventType.STOCK_PROGRAMMED if succeeded else EventType.STOCK_PROGRAMMING_UNCERTAIN)
                    if stock
                    else (EventType.PRICOL_PROGRAMMED if succeeded else EventType.PRICOL_PROGRAMMING_UNCERTAIN)
                )
                self._store.save(batch, [self._event(batch, event_type, slot=number, mac=str(target.mac), **detail)])
                self._activity.pop(number, None)

            self._notify()

        self._parallel(slots, run)

    def _readback_stage(self, batch: Batch, purpose: MacPurpose, token: CancellationToken | None) -> None:
        stock = purpose is MacPurpose.STOCK_RF_TEST

        with self._lock:
            slots = batch.stock_readback_targets if stock else batch.pricol_readback_targets
            targets = {number: self._target(batch, number, purpose) for number in slots}

        programmer = self._programmer
        assert programmer is not None

        def run(number: int) -> None:
            target = targets[number]
            self._set_activity(number, "Verifying MAC readback", None)

            reported = None
            reason = None
            try:
                verify = programmer.verify_stock_mac if stock else programmer.verify_pricol_mac
                result = verify(target, cancellation_token=token)
                if result.verification.outcome is IdentityOutcome.NOT_VERIFIED or result.readback.mac is None:
                    reason = result.verification.message or result.readback.message
                else:
                    reported = result.readback.mac
            except Exception as exc:  # noqa: BLE001 - per-slot isolation
                logger.exception("Readback raised | slot=%s", number)
                reason = f"{type(exc).__name__}: {exc}"

            with self._lock:
                if reported is not None:
                    try:
                        if stock:
                            batch.record_stock_readback(number, str(reported))
                        else:
                            batch.record_pricol_readback(number, str(reported))
                        event = self._event(
                            batch,
                            EventType.STOCK_MAC_READBACK_CONFIRMED if stock else EventType.PRICOL_MAC_READBACK_CONFIRMED,
                            slot=number,
                            expected_mac=str(target.mac),
                            reported_mac=str(reported),
                        )
                    except VerificationMismatch as exc:
                        event = self._event(batch, EventType.HOLD_PLACED, slot=number, reason=str(exc), expected_mac=str(target.mac), reported_mac=str(reported))
                else:
                    if stock:
                        batch.record_stock_readback_unverified(number, reason=reason or "readback failed")
                    else:
                        batch.record_pricol_readback_unverified(number, reason=reason or "readback failed")
                    event = self._event(batch, EventType.HOLD_PLACED, slot=number, reason=batch.slots[number].hold_reason, expected_mac=str(target.mac))

                self._store.save(batch, [event])
                self._activity.pop(number, None)

            self._notify()

        self._parallel(slots, run)

    def _rf_stage(self, batch: Batch, token: CancellationToken | None) -> None:
        with self._lock:
            targets = self._rf_eligibility.build_targets(batch)
            for target in targets:
                self._activity[target.slot_number] = SlotActivity("Golden Rig RF test")

        self._notify()

        try:
            report = self._rf_tester.run_test(
                batch_id=batch.batch_id,
                station_id=batch.station_id,
                jig_id=batch.jig_id,
                targets=targets,
                cancel_event=token.event if token is not None else None,
            )
        except Exception as exc:
            # The batch stays in STOCK_RF_TESTING with no slot changed,
            # so the operator may retry once the rig is restored.
            logger.exception("Golden Rig RF test failed | batch_id=%s", batch.batch_id)
            raise ProductionError(
                f"Golden Rig RF test could not run: {exc}. "
                "Supervisor intervention required; press RETRY once the rig is available."
            ) from exc

        with self._lock:
            self._rf_processor.apply(batch, report)
            events = []
            for result in report.response.results:
                events.append(self._event(
                    batch,
                    EventType.STOCK_RF_RESULT,
                    slot=result.slot_number,
                    request_id=report.request.request_id,
                    rig_id=report.response.rig_id,
                    outcome=result.outcome.value,
                    expected_mac=str(result.expected_mac),
                    reported_mac=str(result.reported_mac) if result.reported_mac else None,
                    connected=result.connected,
                    disconnected=result.disconnected,
                    error_code=result.error_code,
                    detail=result.detail,
                ))
                if batch.slots[result.slot_number].state is DeviceState.STOCK_RF_CONFIRMED:
                    events.append(self._event(batch, EventType.STOCK_RF_CONFIRMED, slot=result.slot_number, mac=str(result.expected_mac)))
            self._store.save(batch, events)
            self._activity = {}

    def _after_rf(self, batch: Batch) -> None:
        with self._lock:
            if not batch.requires_pricol:
                batch.begin_qr_scanning()
                self._store.save(batch, [self._event(batch, EventType.QR_SCANNING_STARTED, slots=[slot.number for slot in batch.qr_candidates])])
                return

            eligible = tuple(slot.number for slot in batch.pricol_eligible_slots)
            try:
                records = self._store.reserve_macs(batch, MacPurpose.PRICOL_PRODUCTION, eligible)
            except Exception as exc:
                raise ProductionError(
                    f"Pricol MAC reservation failed: {exc}. Refill the authorized MAC cache, then press RETRY."
                ) from exc

            batch.accept_pricol_reservations(records)
            self._store.save(batch, [
                self._event(batch, EventType.PRICOL_MAC_RESERVED, slot=record.slot_number, mac=str(record.address), allocation_id=record.allocation_id)
                for record in records
            ])

    def _functional_stage(self, batch: Batch, *, dfu: bool, token: CancellationToken | None) -> None:
        with self._lock:
            slots = batch.functional_dfu_targets if dfu else batch.functional_app_targets
            ports = {number: self._ports[number].com_port for number in slots}

        label = "DFU test" if dfu else "Application test"

        def run(number: int) -> None:
            self._set_activity(number, label, None)
            evidence: dict[str, Any] = {}
            try:
                test = self._functional.run_dfu_test if dfu else self._functional.run_app_test
                result = test(slot_number=number, com_port=ports[number], cancellation_token=token)
                if isinstance(result, FunctionalTestOutcome):
                    passed = result.passed
                    reason = None if passed else result.detail
                    evidence = {
                        "command": result.command,
                        "expected": result.expected,
                        "response": result.response,
                        "detail": result.detail,
                        "duration_seconds": result.duration_seconds,
                    }
                else:
                    passed = result
                    reason = None if passed is not None else "no trustworthy test result"
            except Exception as exc:  # noqa: BLE001 - per-slot isolation
                logger.exception("Functional test raised | slot=%s", number)
                passed = None
                reason = f"{type(exc).__name__}: {exc}"

            with self._lock:
                if passed is None:
                    if dfu:
                        batch.record_pricol_dfu_unverified(number, reason=reason or "unverified")
                    else:
                        batch.record_pricol_app_unverified(number, reason=reason or "unverified")
                elif dfu:
                    batch.record_pricol_dfu(number, passed)
                else:
                    batch.record_pricol_app(number, passed)

                self._store.save(batch, [self._event(
                    batch,
                    EventType.PRICOLDFU_RESULT if dfu else EventType.PRICOLAPP_RESULT,
                    slot=number,
                    passed=passed,
                    reason=reason,
                    com_port=ports[number],
                    **evidence,
                )])
                self._activity.pop(number, None)

            self._notify()

        self._parallel(slots, run)

    def _finalise(self, batch: Batch) -> None:
        """Final validation, commit, and queue for server upload."""
        if not batch.can_commit:
            raise ProductionError(
                "Final validation failed: a slot is missing a mandatory "
                "programming, verification or QR record"
            )

        batch.commit()
        events = list(self._disposition_events(batch))
        events.append(self._event(batch, EventType.BATCH_COMMITTED, dispositions={str(number): value.value for number, value in batch.dispositions.items()}))
        batch.queue_upload()
        events.append(self._event(batch, EventType.UPLOAD_QUEUED))
        self._store.save_and_enqueue(batch, events, build_batch_record(batch))
        logger.info("Batch committed and queued | batch_id=%s", batch.batch_id)
        for callback in tuple(self._commit_listeners):
            try:
                callback(batch)
            except Exception:  # noqa: BLE001 - upload problems never fail production
                logger.exception("Commit listener failed")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parallel(self, slots: tuple[int, ...], run: Callable[[int], None]) -> None:
        """One worker per eligible slot; a failing slot never stops others."""
        if not slots:
            return

        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=len(slots), thread_name_prefix="slot") as pool:
            futures = [pool.submit(run, number) for number in slots]
            for future in futures:
                exc = future.exception()
                if exc is not None:
                    errors.append(exc)

        if errors:
            # A domain/persistence error after the external call is a
            # controller defect, not a DUT failure. Surface it.
            raise errors[0]

    def _target(self, batch: Batch, number: int, purpose: MacPurpose) -> ProgrammingTarget:
        slot = batch.slots[number]
        mac = slot.stock_mac if purpose is MacPurpose.STOCK_RF_TEST else slot.pricol_mac
        assert mac is not None
        return ProgrammingTarget(
            batch_id=batch.batch_id,
            slot_number=number,
            com_port=self._ports[number].com_port,
            mac=mac,
            purpose=purpose,
        )

    def _set_activity(self, number: int, text: str, progress: int | None) -> None:
        with self._lock:
            self._activity[number] = SlotActivity(text, progress)
        self._notify()

    def _fail(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        logger.warning("Production step refused/failed: %s", message)

    def _require_access(self) -> tuple[str, str]:
        assert self._access is not None
        try:
            return self._access.require_production_access()
        except AccessError as exc:
            raise ProductionError(str(exc)) from exc

    def _require_batch(self) -> Batch:
        if self._batch is None:
            raise ProductionError("No active batch")
        return self._batch

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - a listener must not break production
                logger.exception("Production listener failed")

    def _event(self, batch: Batch, event_type: EventType, *, slot: int | None = None, **payload: Any) -> ManufacturingEvent:
        module_qr = batch.slots[slot].module_qr if slot is not None else None
        return ManufacturingEvent(
            event_type=event_type,
            batch_id=batch.batch_id,
            station_id=batch.station_id,
            jig_id=batch.jig_id,
            slot_number=slot,
            module_qr=module_qr,
            payload=payload,
        )

    def _disposition_events(self, batch: Batch) -> list[ManufacturingEvent]:
        return [
            self._event(batch, EventType.SLOT_DISPOSITION, slot=number, disposition=disposition.value, hold_reason=batch.slots[number].hold_reason)
            for number, disposition in batch.dispositions.items()
            if disposition is not SlotDisposition.EMPTY
        ]


def _firmware_identity(path: Path | None) -> dict[str, str] | None:
    """Path and SHA-256 of a firmware image, for the traceability record."""
    if path is None:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"path": str(path), "sha256": digest.hexdigest()}


def build_batch_record(batch: Batch) -> dict[str, Any]:
    """Self-contained upload record preserving original identity and time."""
    slots = []
    for slot in batch.ordered_slots:
        rf = slot.golden_rf_result
        slots.append({
            "slot_number": slot.number,
            "disposition": batch.disposition_for(slot.number).value,
            "device_state": slot.state.value,
            "port_identity": slot.port_identity,
            "module_qr": slot.module_qr,
            "stock_mac": str(slot.stock_mac) if slot.stock_mac else None,
            "stock_readback_mac": str(slot.stock_readback_mac) if slot.stock_readback_mac else None,
            "stock_rf_outcome": rf.outcome.value if rf is not None else None,
            "pricol_mac": str(slot.pricol_mac) if slot.pricol_mac else None,
            "pricol_readback_mac": str(slot.pricol_readback_mac) if slot.pricol_readback_mac else None,
            "pricol_app_passed": slot.functional.pricol_app_passed,
            "pricol_dfu_passed": slot.functional.pricol_dfu_passed,
            "hold_reason": slot.hold_reason,
        })

    return {
        "schema_version": 1,
        "batch_id": batch.batch_id,
        "batch_label": batch.batch_label,
        "station_id": batch.station_id,
        "jig_id": batch.jig_id,
        "operator_id": batch.operator_id,
        "session_id": batch.session_id,
        "programming_mode": batch.programming_mode.value,
        "state": batch.state.value,
        "created_at": batch.created_at.isoformat(),
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "slots": slots,
    }
