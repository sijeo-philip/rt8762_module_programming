from stationapp.domain.batch import (
    Batch,
    BatchState,
)
from stationapp.services.workflow import (
    OperatorAction,
    WorkflowService,
)


def make_batch() -> Batch:

    return Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=8,
        batch_id="BATCH-001",
    )


def test_created_batch_requests_load_modules() -> None:

    batch = make_batch()

    view = WorkflowService().describe(
        batch
    )

    assert view.action is OperatorAction.LOAD_MODULES

    assert view.allow_start_button is True

    assert view.scanner_enabled is False


def test_automatic_operation_has_no_operator_button() -> None:

    batch = make_batch()

    batch.state = BatchState.STOCK_PROGRAMMING

    view = WorkflowService().describe(
        batch
    )

    assert view.action is OperatorAction.WAIT

    assert view.automatic_operation is True

    assert view.allow_start_button is False


def test_qr_scanning_exposes_expected_slot() -> None:

    batch = make_batch()

    batch.state = BatchState.QR_SCANNING

    view = WorkflowService().describe(
        batch
    )

    assert view.action is OperatorAction.SCAN_QR

    assert view.expected_slot == 1

    assert view.scanner_enabled is True


def test_qr_workflow_advances_using_domain_slot() -> None:

    batch = make_batch()

    batch.state = BatchState.QR_SCANNING

    # For this projection test we mark first slot as already scanned.
    batch.slots[1].module_qr = "QR-0001"

    view = WorkflowService().describe(
        batch
    )

    assert view.expected_slot == 2

    assert "SLOT 2" in view.instruction


def test_hold_requires_supervisor() -> None:

    batch = make_batch()

    batch.state = BatchState.HOLD
    batch.hold_reason = "MAC verification failed"

    view = WorkflowService().describe(
        batch
    )

    assert (
        view.action
        is OperatorAction.CALL_SUPERVISOR
    )

    assert view.requires_supervisor is True


def test_committed_batch_requests_module_removal() -> None:

    batch = make_batch()

    batch.state = BatchState.COMMITTED

    view = WorkflowService().describe(
        batch
    )

    assert (
        view.action
        is OperatorAction.REMOVE_COMPLETED_BATCH
    )

    assert view.completed is True

    