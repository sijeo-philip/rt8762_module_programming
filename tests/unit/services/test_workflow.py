from stationapp.domain.batch import (
    Batch,
    BatchState,
)
from stationapp.services.workflow import (
    OperatorAction,
    WorkflowService,
)

from stationapp.domain.slot import DeviceState, FunctionalResults
from stationapp.domain.mac import AllocatedMac, MacAddress, MacPurpose

def make_batch() -> Batch:

    return Batch(
        station_id="STATION-01",
        jig_id="JIG-01",
        slot_count=8,
        batch_id="BATCH-001",
    )

def make_qr_scanning_batch() -> Batch:
    """Prepare QR-eligible slots for workflow projection tests."""

    batch = make_batch()
    batch.configure_occupied_slots((1, 2, 4, 7))

    # These are fixture-only states. Actual production transitions
    # are covered in the domain and Golden Rig service tests.
    for number in (1, 2, 4):
        slot = batch.slots[number]

        slot.state = DeviceState.FUNCTIONAL_TEST_PASSED
        slot.functional = FunctionalResults(
            pricol_app_passed=True,
            pricol_dfu_passed=True,
        )

        stock = AllocatedMac(
            allocation_id=f"STOCK-{number}",
            address=MacAddress.parse(
                f"AA:BB:CC:00:00:{number:02X}"
            ),
            purpose=MacPurpose.STOCK_RF_TEST,
        )
        stock.reserve(batch_id=batch.batch_id, slot_number=number)
        stock.begin_programming()
        stock.mark_issued()
        stock.confirm()

        pricol = AllocatedMac(
            allocation_id=f"PRICOL-{number}",
            address=MacAddress.parse(
                f"DD:EE:FF:00:00:{number:02X}"
            ),
            purpose=MacPurpose.PRICOL_PRODUCTION,
        )
        pricol.reserve(batch_id=batch.batch_id, slot_number=number)
        pricol.begin_programming()
        pricol.mark_issued()
        pricol.confirm()

        slot.stock_mac_record = stock
        slot.pricol_mac_record = pricol

    # Slot 7 is physically occupied but quarantined.
    batch.slots[7].place_on_hold(
        "Golden Rig RF verification failed"
    )

    batch.state = BatchState.QR_SCANNING
    return batch



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
    batch = make_qr_scanning_batch()

    view = WorkflowService().describe(batch)

    assert view.action is OperatorAction.SCAN_QR
    assert view.expected_slot == 1
    assert view.scanner_enabled is True
    assert "SLOT 1" in view.instruction


def test_qr_workflow_advances_using_domain_slot() -> None:
    batch = make_qr_scanning_batch()

    # Fixture-only: indicate Slot 1 was scanned.
    batch.slots[1].module_qr = "QR-0001"

    view = WorkflowService().describe(batch)

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

def test_qr_workflow_skips_empty_slots_and_scans_held_ones() -> None:
    batch = make_qr_scanning_batch()

    batch.slots[1].module_qr = "QR-0001"
    batch.slots[2].module_qr = "QR-0002"

    view = WorkflowService().describe(batch)

    # Slot 3 is empty, so the next scan is Slot 4.
    assert view.expected_slot == 4
    assert "SLOT 4" in view.instruction

    # Slot 7 is held: its QR is still scanned and recorded.
    batch.slots[4].module_qr = "QR-0004"

    assert batch.next_qr_slot == 7
    view = WorkflowService().describe(batch)
    assert "FAILED module in SLOT 7" in view.instruction

def test_qr_scanning_without_candidate_requires_supervisor():
    batch = make_batch()
    batch.state = BatchState.QR_SCANNING

    view = WorkflowService().describe(batch)

    assert view.action is OperatorAction.CALL_SUPERVISOR
    assert view.expected_slot is None
    assert view.scanner_enabled is False
    assert view.requires_supervisor is True
    assert "SLOT None" not in view.instruction
