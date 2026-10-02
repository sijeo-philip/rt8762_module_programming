"""Operator-facing production workflow projection.

The GUI must not derive manufacturing rules itself.

This module translates domain BatchState values into simple,
operator-facing instructions such as:

    LOAD MODULES
    PRESS START
    WAIT - PROGRAMMING
    SCAN SLOT 3
    CALL SUPERVISOR

The operator interface can therefore remain simple and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stationapp.domain.batch import (
    Batch,
    BatchState,
)


class OperatorAction(str, Enum):
    """The next meaningful action exposed to the operator."""

    LOAD_MODULES = "LOAD_MODULES"

    START_BATCH = "START_BATCH"

    WAIT = "WAIT"

    CHANGE_TO_STOCK_PROGRAM_MODE = ("CHANGE_TO_STOCK_PROGRAM_MODE")

    CHANGE_TO_STOCK_RF_MODE = ("CHANGE_TO_STOCK_RF_MODE")

    CHANGE_TO_PRICOL_PROGRAM_MODE = ("CHANGE_TO_PRICOL_PROGRAM_MODE")
    
    CHANGE_TO_FUNCTIONAL_TEST_MODE = ("CHANGE_TO_FUNCTIONAL_TEST_MODE")

    RESET_FOR_PRICOL_APP = ("RESET_FOR_PRICOL_APP")

    RESET_FOR_PRICOL_DFU = ("RESET_FOR_PRICOL_DFU" )

    SCAN_QR = "SCAN_QR"

    REMOVE_COMPLETED_BATCH = ("REMOVE_COMPLETED_BATCH")

    CALL_SUPERVISOR = "CALL_SUPERVISOR"

    NONE = "NONE"


@dataclass(
    frozen=True,
    slots=True,
)
class OperatorWorkflow:
    """Simple immutable projection consumed by the GUI."""

    batch_id: str
    batch_state: BatchState
    action: OperatorAction
    headline: str
    instruction: str
    expected_slot: int | None = None
    allow_start_button: bool = False
    allow_cancel_button: bool = False
    scanner_enabled: bool = False
    automatic_operation: bool = False
    completed: bool = False
    requires_supervisor: bool = False

class WorkflowService:
    """Translate domain batch state into operator-facing workflow."""

    def describe(self, batch: Batch) -> OperatorWorkflow:

        state = batch.state

        if state is BatchState.CREATED:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.LOAD_MODULES,
                headline="LOAD MODULES",
                instruction=(
                    f"Load all {batch.slot_count} modules "
                    "into the jig."
                ),
                allow_start_button=True,
            )

        if state is BatchState.PORT_BINDING:
            return self._automatic(
                batch,
                headline="CHECKING JIG",
                instruction=(
                    "Detecting and assigning jig ports."
                ),
            )

        if state is BatchState.PORTS_BOUND:
            return self._automatic(
                batch,
                headline="PREPARING BATCH",
                instruction=(
                    "Reserving authorized Stock MAC "
                    "addresses from the local cache."
                ),
            )

        if state is BatchState.STOCK_MACS_RESERVED:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_STOCK_PROGRAM_MODE
                ),
                headline="PREPARE FOR STOCK PROGRAMMING",
                instruction=(
                    "Set the jig/module connection as shown "
                    "for Stock programming."
                ),
                allow_start_button=True,
            )

        if state is BatchState.AWAITING_STOCK_PROGRAM_MODE:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_STOCK_PROGRAM_MODE
                ),
                headline="STOCK PROGRAM MODE",
                instruction=(
                    "Complete the indicated jig setup, "
                    "then press CONTINUE."
                ),
                allow_start_button=True,
            )

        if state is BatchState.STOCK_PROGRAMMING:
            return self._automatic(
                batch,
                headline="PROGRAMMING MODULES",
                instruction=(
                    "Stock firmware programming in progress. "
                    "Do not remove modules."
                ),
            )

        if state is BatchState.STOCK_PROGRAMMED:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_STOCK_RF_MODE
                ),
                headline="PREPARE FOR RF TEST",
                instruction=(
                    "Set the jig for Stock RF verification."
                ),
                allow_start_button=True,
            )

        if state is BatchState.AWAITING_STOCK_RF_MODE:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_STOCK_RF_MODE
                ),
                headline="RF TEST MODE",
                instruction=(
                    "Complete the indicated setup, "
                    "then press CONTINUE."
                ),
                allow_start_button=True,
            )

        if state is BatchState.STOCK_RF_TESTING:
            return self._automatic(
                batch,
                headline="TESTING MODULES",
                instruction=(
                    "Stock RF verification in progress."
                ),
            )

        if state is BatchState.STOCK_RF_CONFIRMED:
            return self._automatic(
                batch,
                headline="PREPARING PRODUCTION MACS",
                instruction=(
                    "Reserving authorized Pricol MAC "
                    "addresses from the local cache."
                ),
            )

        if state is BatchState.PRICOL_MACS_RESERVED:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_PRICOL_PROGRAM_MODE
                ),
                headline="PREPARE FOR FINAL PROGRAMMING",
                instruction=(
                    "Set the jig for Pricol programming."
                ),
                allow_start_button=True,
            )

        if state is BatchState.AWAITING_PRICOL_PROGRAM_MODE:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_PRICOL_PROGRAM_MODE
                ),
                headline="FINAL PROGRAM MODE",
                instruction=(
                    "Complete the indicated setup, "
                    "then press CONTINUE."
                ),
                allow_start_button=True,
            )

        if state is BatchState.PRICOL_PROGRAMMING:
            return self._automatic(
                batch,
                headline="FINAL PROGRAMMING",
                instruction=(
                    "Pricol firmware/MAC programming "
                    "in progress."
                ),
            )

        if state is BatchState.PRICOL_PROGRAMMED:
            return self._automatic(
                batch,
                headline="VERIFYING MAC ADDRESSES",
                instruction=(
                    "Reading programmed MAC addresses."
                ),
            )

        if state is BatchState.PRICOL_READBACK_VERIFIED:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_FUNCTIONAL_TEST_MODE
                ),
                headline="PREPARE FOR FUNCTIONAL TEST",
                instruction=(
                    "Set the jig for functional testing."
                ),
                allow_start_button=True,
            )

        if state is BatchState.AWAITING_FUNCTIONAL_TEST_MODE:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_FUNCTIONAL_TEST_MODE
                ),
                headline="FUNCTIONAL TEST MODE",
                instruction=(
                    "Complete the indicated setup, "
                    "then press CONTINUE."
                ),
                allow_start_button=True,
            )

        if state is BatchState.AWAITING_PRICOLAPP_RESET:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.RESET_FOR_PRICOL_APP,
                headline="RESET MODULES",
                instruction=(
                    "Reset the jig as indicated for "
                    "the application test."
                ),
                allow_start_button=True,
            )

        if state is BatchState.PRICOLAPP_TESTING:
            return self._automatic(
                batch,
                headline="RUNNING APPLICATION TEST",
                instruction=(
                    "Functional test in progress."
                ),
            )

        if state is BatchState.AWAITING_PRICOLDFU_RESET:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.RESET_FOR_PRICOL_DFU,
                headline="RESET MODULES",
                instruction=(
                    "Reset the jig as indicated for "
                    "the DFU test."
                ),
                allow_start_button=True,
            )

        if state is BatchState.PRICOLDFU_TESTING:
            return self._automatic(
                batch,
                headline="RUNNING DFU TEST",
                instruction=(
                    "DFU functional test in progress."
                ),
            )

        if state is BatchState.FUNCTIONAL_TEST_COMPLETE:
            return self._automatic(
                batch,
                headline="TESTS COMPLETE",
                instruction=(
                    "Preparing QR scanning."
                ),
            )

        if state is BatchState.QR_SCANNING:

            slot_number = batch.next_qr_slot

            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.SCAN_QR,
                headline="SCAN MODULE QR",
                instruction=(
                    f"Scan the QR on the module "
                    f"in SLOT {slot_number}."
                ),
                expected_slot=slot_number,
                scanner_enabled=True,
            )

        if state is BatchState.QR_BOUND:
            return self._automatic(
                batch,
                headline="FINAL CHECK",
                instruction=(
                    "All QRs scanned. "
                    "Completing final validation."
                ),
            )

        if state in {
            BatchState.COMMITTED,
            BatchState.UPLOAD_PENDING,
            BatchState.UPLOADED,
        }:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.REMOVE_COMPLETED_BATCH
                ),
                headline="BATCH COMPLETE",
                instruction=(
                    "Remove completed modules and "
                    "load the next batch."
                ),
                completed=True,
            )

        if state in {
            BatchState.HOLD,
            BatchState.ABORTED,
        }:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.CALL_SUPERVISOR,
                headline="SUPERVISOR REQUIRED",
                instruction=(
                    batch.hold_reason
                    or "This batch requires supervisor review."
                ),
                requires_supervisor=True,
            )

        return OperatorWorkflow(
            batch_id=batch.batch_id,
            batch_state=state,
            action=OperatorAction.NONE,
            headline="PLEASE WAIT",
            instruction="Preparing next operation.",
        )

    @staticmethod
    def _automatic(
        batch: Batch,
        *,
        headline: str,
        instruction: str,
    ) -> OperatorWorkflow:

        return OperatorWorkflow(
            batch_id=batch.batch_id,
            batch_state=batch.state,
            action=OperatorAction.WAIT,
            headline=headline,
            instruction=instruction,
            automatic_operation=True,
        )

    

    