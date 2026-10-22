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

from dataclasses import dataclass, replace
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

    COMPLETE_BATCH = "COMPLETE_BATCH"

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
    # The station is working but needs the operator to act now (e.g.
    # reset the modules while it listens for their start-up message).
    operator_action_now: bool = False
    completed: bool = False
    requires_supervisor: bool = False
    button_label: str = "CONTINUE"

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
                button_label="START",
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
            return self._automatic(
                batch, 
                headline="VERIFYING STOCK MACS",
                instruction=(
                    "Reading Stock MAC addresses through MPCLI "
                    "and verifying each address aganist its "
                    "reserved identity. Do not remove modules."
                ),
            )

        if state in (BatchState.STOCK_READBACK_VERIFIED, BatchState.AWAITING_STOCK_RF_MODE):
            # Modules are still in boot (download) mode after programming;
            # the RF test needs them running the application.
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_STOCK_RF_MODE
                ),
                headline="RESET MODULES FOR RF TEST",
                instruction=(
                    "Programming and MAC readback passed. Switch every "
                    "module from boot mode to application mode and reset "
                    "it, then press RUN RF TEST."
                ),
                allow_start_button=True,
                button_label="RUN RF TEST",
            )

        if state is BatchState.STOCK_RF_TESTING:
            return self._automatic(
                batch,
                headline="TESTING MODULES",
                instruction=(
                    "Stock RF verification in progress."
                ),
            )

        if state is BatchState.STOCK_RF_CONFIRMED and not batch.requires_pricol:
            return self._automatic(
                batch,
                headline="RF TEST COMPLETE",
                instruction=(
                    "Stock-only batch: preparing QR scanning."
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

        if state in (BatchState.PRICOL_MACS_RESERVED, BatchState.AWAITING_PRICOL_PROGRAM_MODE):
            # After the RF test the modules run the application; flashing
            # needs them back in boot (download) mode.
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=(
                    OperatorAction.CHANGE_TO_PRICOL_PROGRAM_MODE
                ),
                headline="SWITCH MODULES TO BOOT MODE",
                instruction=(
                    "RF test complete. Switch every module to boot "
                    "(download) mode and reset it, then press "
                    "PROGRAM PRICOL FIRMWARE."
                ),
                allow_start_button=True,
                button_label="PROGRAM PRICOL FIRMWARE",
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

        if state in (
            BatchState.PRICOL_READBACK_VERIFIED,
            BatchState.AWAITING_FUNCTIONAL_TEST_MODE,
            BatchState.AWAITING_PRICOLAPP_RESET,
        ):
            # After Pricol programming the modules are in boot mode; the
            # functional test needs them running the application.
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.RESET_FOR_PRICOL_APP,
                headline="SWITCH MODULES TO APPLICATION MODE",
                instruction=(
                    "Pricol firmware and MAC verified. Switch every "
                    "module from boot mode to application mode, then "
                    "press RUN APP TEST. When told, reset (power-cycle) "
                    "the modules: the station waits for each module's "
                    "start-up message, then sends PRICOLAPP."
                ),
                allow_start_button=True,
                button_label="RUN APP TEST",
            )

        if state is BatchState.PRICOLAPP_TESTING:
            return replace(
                self._automatic(
                    batch,
                    headline="RESET ALL MODULES NOW",
                    instruction=(
                        "Reset (power-cycle) every module now. The station "
                        "waits for each module's start-up message, then "
                        "sends PRICOLAPP."
                    ),
                ),
                operator_action_now=True,
            )

        if state is BatchState.AWAITING_PRICOLDFU_RESET:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.RESET_FOR_PRICOL_DFU,
                headline="READY FOR DFU TEST",
                instruction=(
                    "Press RUN DFU TEST, then reset all modules again "
                    "when told. The station waits for each module's "
                    "start-up message, then sends PRICOLDFU."
                ),
                allow_start_button=True,
                button_label="RUN DFU TEST",
            )

        if state is BatchState.PRICOLDFU_TESTING:
            return replace(
                self._automatic(
                    batch,
                    headline="RESET ALL MODULES NOW",
                    instruction=(
                        "Reset (power-cycle) every module now. The station "
                        "waits for each module's start-up message, then "
                        "sends PRICOLDFU."
                    ),
                ),
                operator_action_now=True,
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

            if slot_number is None:
                return OperatorWorkflow(
                    batch_id=batch.batch_id,
                    batch_state=state,
                    action=OperatorAction.CALL_SUPERVISOR,
                    headline="QR SCANNING STATE INCONSISTENT",
                    instruction=(
                        "No module remains for QR scanning, "
                        "but the batch is still in QR_SCANNING. "
                        "Request supervisor review."
                    ),
                    scanner_enabled=False,
                    requires_supervisor=True,
                )

            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.SCAN_QR,
                headline="SCAN MODULE QR",
                instruction=(
                    f"Scan the QR on the FAILED module in SLOT {slot_number}, "
                    "then keep it apart from the good modules."
                    if batch.slots[slot_number].is_held
                    else f"Scan the QR on the module in SLOT {slot_number}."
                ),
                expected_slot=slot_number,
                scanner_enabled=True,
                button_label="SCANNING...",
            )

        if state is BatchState.QR_BOUND:
            return OperatorWorkflow(
                batch_id=batch.batch_id,
                batch_state=state,
                action=OperatorAction.COMPLETE_BATCH,
                headline="FINAL VALIDATION",
                instruction=(
                    "All eligible modules are scanned. Review the "
                    "slot results, then press COMPLETE BATCH to "
                    "commit the records and queue them for upload."
                ),
                allow_start_button=True,
                button_label="COMPLETE BATCH",
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
                button_label="PREPARE NEXT BATCH",
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

    

    