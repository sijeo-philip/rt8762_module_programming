
import pytest

from stationapp.services.jig_status_projection import (
    JigStatusProjection,
)

from stationapp.domain.batch import Batch

from stationapp.ui.widgets.jig_slot_grid import (
    JigSlotGrid,
)


@pytest.mark.unit
def test_eight_up_grid(qtbot):
    grid = JigSlotGrid(8)
    qtbot.addWidget(grid)

    assert len(grid.cards) == 8

    for number in range(1, 9):
        assert number in grid.cards


@pytest.mark.unit
def test_four_up_grid(qtbot):
    grid = JigSlotGrid(4)
    qtbot.addWidget(grid)

    assert len(grid.cards) == 4


@pytest.mark.unit
def test_partial_batch_renders(qtbot):
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-8UP",
        slot_count=8,
        batch_id="BATCH-VISUAL-001",
    )
    batch.configure_occupied_slots((1, 2, 4, 7))

    batch.slots[1].bind_port("USB-SERIAL-001")

    view = JigStatusProjection().project(batch)

    grid = JigSlotGrid(8)
    qtbot.addWidget(grid)

    grid.update_view(view)

    assert grid.cards[1].status_label.text() == "READY"
    assert grid.cards[3].status_label.text() == "EMPTY"
    assert grid.cards[5].status_label.text() == "EMPTY"

    assert "BATCH-VISUAL-001" in grid.summary_label.text()
    assert "Occupied: 4/8" in grid.summary_label.text()


@pytest.mark.unit
def test_hold_reason_is_available_as_tooltip(qtbot):
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-4UP",
        slot_count=4,
    )
    batch.slots[2].bind_port("USB-SERIAL-002")
    batch.slots[2].place_on_hold(
        "Golden Rig identity uncertain"
    )

    grid = JigSlotGrid(4)
    qtbot.addWidget(grid)

    grid.update_view(
        JigStatusProjection().project(batch)
    )

    assert grid.cards[2].status_label.text() == "HOLD"
    assert "identity uncertain" in grid.cards[2].toolTip()


@pytest.mark.unit
def test_grid_rejects_incomplete_snapshot(qtbot):
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-4UP",
        slot_count=4,
    )

    view = JigStatusProjection().project(batch)
    grid = JigSlotGrid(8)
    qtbot.addWidget(grid)

    with pytest.raises(ValueError):
        grid.update_view(view)


@pytest.mark.unit
def test_clear_batch_removes_previous_status(qtbot):
    batch = Batch(
        station_id="STATION-01",
        jig_id="JIG-4UP",
        slot_count=4,
    )
    batch.slots[1].bind_port("USB-SERIAL-001")

    grid = JigSlotGrid(4)
    qtbot.addWidget(grid)

    grid.update_view(
        JigStatusProjection().project(batch)
    )

    assert grid.cards[1].status_label.text() == "READY"

    grid.clear_batch()

    assert grid.cards[1].status_label.text() == "EMPTY"
    assert grid.summary_label.text() == "No active production batch"
