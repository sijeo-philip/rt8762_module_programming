
import pytest

from stationapp.domain.batch import Batch
from stationapp.domain.slot import DeviceState

from stationapp.services.jig_status_projection import (
    JigStatusProjection,
    SlotVisualStatus,
)


def make_batch():
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-8UP",
        slot_count=8,
        batch_id="BATCH-UI-001",
    )

    batch.configure_occupied_slots((1, 2, 4, 7))
    return batch


@pytest.mark.unit
def test_partial_jig_displays_all_physical_slots():
    batch = make_batch()

    view = JigStatusProjection().project(batch)

    assert len(view.slots) == 8
    assert view.occupied_count == 4

    assert [ slot.number for slot in view.slots] == list(range(1, 9))

    for number in (3, 5, 6, 8):
        assert (view.slots[number - 1].status is SlotVisualStatus.EMPTY)


@pytest.mark.unit
def test_port_bound_slot_displays_ready():
    batch = make_batch()

    batch.slots[1].bind_port("USB-SERIAL-001")

    view = JigStatusProjection().project(batch)

    assert view.slots[0].status is SlotVisualStatus.READY
    assert view.slots[0].stage == "Port bound"


@pytest.mark.unit
def test_hold_is_independent():
    batch = make_batch()

    batch.slots[2].bind_port("USB-SERIAL-002")
    batch.slots[2].place_on_hold("Stock programming failed")

    view = JigStatusProjection().project(batch)

    assert view.slots[1].status is SlotVisualStatus.HOLD
    assert "programming failed" in view.slots[1].detail
    assert view.held_count == 1

    assert view.slots[2].status is SlotVisualStatus.EMPTY


@pytest.mark.unit
def test_projection_does_not_mutate_batch():
    batch = make_batch()

    before = tuple(slot.state for slot in batch.ordered_slots)

    JigStatusProjection().project(batch)

    after = tuple(slot.state for slot in batch.ordered_slots)

    assert before == after
    assert all(state is DeviceState.EMPTY for state in after)


@pytest.mark.unit
def test_functional_failure_has_distinct_status():
    batch = make_batch()

    # Projection-only fixture; production transitions
    # remain enforced by JigSlot methods.
    batch.slots[4].state = DeviceState.FUNCTIONAL_TEST_FAILED

    view = JigStatusProjection().project(batch)

    assert (view.slots[3].status is SlotVisualStatus.FUNCTIONAL_FAIL)


@pytest.mark.unit
def test_qr_binding_does_not_hide_functional_failure():
    from stationapp.domain.slot import FunctionalResults

    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-4UP",
        slot_count=4,
    )

    slot = batch.slots[2]

    # Projection-specific fixture.
    slot.state = DeviceState.QR_BOUND
    slot.module_qr = "MODULE-002"
    slot.functional = FunctionalResults(
        pricol_app_passed=False,
        pricol_dfu_passed=True,
    )

    view = JigStatusProjection().project(batch)

    assert (
        view.slots[1].status
        is SlotVisualStatus.FUNCTIONAL_FAIL
    )


    