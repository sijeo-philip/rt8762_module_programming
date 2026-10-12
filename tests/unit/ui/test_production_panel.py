"""Operator panel driven through a full batch with simulated hardware."""

from __future__ import annotations

from pathlib import Path

import pytest

from stationapp.concurrency.manager import OperationManager
from stationapp.config import Settings
from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import BatchState
from stationapp.services.health import HealthCheck, Severity
from stationapp.services.mac_cache import MacCacheService
from stationapp.services.production import ProductionController
from stationapp.services.readiness import StationReadinessService
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedMacAllocationGateway,
    SimulatedProgrammer,
)
from stationapp.services.slot_eligibility import SlotEligibilityService
from stationapp.ui.widgets.jig_slot_grid import JigSlotGrid
from stationapp.ui.widgets.production_panel import ProductionPanel


@pytest.fixture
def panel(qtbot, tmp_path: Path):
    settings = Settings(station_id="STATION-01", jig_id="JIG-01", jig_positions=4, simulation_mode=True, mac_cache_threshold=8)
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    mac_cache = MacCacheService(
        session_factory=factory,
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        jig_positions=4,
        threshold=8,
        gateway=SimulatedMacAllocationGateway(),
        request_size=16,
    )
    readiness = StationReadinessService(
        settings=settings,
        topology_service=None,
        eligibility_service=SlotEligibilityService(),
        mac_cache=mac_cache,
        golden_rig_check=lambda: HealthCheck("Golden Rig", Severity.WARN, "simulated"),
        simulation=True,
    )
    none = frozenset()
    controller = ProductionController(
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        slot_count=4,
        store=SqlProductionStore(factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=none),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=none),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=none),
        readiness_gate=readiness.gate,
    )
    operations = OperationManager()
    widget = ProductionPanel(
        controller=controller,
        readiness=readiness,
        operations=operations,
        jig_grid=JigSlotGrid(4),
        station_id=settings.station_id,
        simulation=True,
    )
    qtbot.addWidget(widget)
    yield widget, controller, operations
    operations.cancel_all()
    operations.wait_for_all(5_000)
    engine.dispose()


def press_until(qtbot, widget, controller, state: BatchState) -> None:
    for _ in range(15):
        if controller.batch.state is state:
            return
        qtbot.waitUntil(widget.primary_button.isEnabled, timeout=5_000)
        widget.primary_button.click()
        qtbot.waitUntil(lambda: not controller.snapshot().running and widget.primary_button.isEnabled() or controller.batch.state is state, timeout=5_000)
    assert controller.batch.state is state


@pytest.mark.unit
def test_start_is_disabled_until_station_is_ready(panel, qtbot):
    widget, controller, _ = panel

    assert widget.primary_button.text() == "START"
    assert not widget.primary_button.isEnabled()
    assert not widget.qr_input.isEnabled()

    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget.headline_label.text() == "READY - PRESS START", timeout=5_000)

    assert widget.selected_slots == (1, 2, 3, 4)
    assert widget.primary_button.isEnabled()


@pytest.mark.unit
def test_operator_runs_stock_only_batch_end_to_end(panel, qtbot):
    widget, controller, _ = panel

    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled(), timeout=5_000)

    widget.stock_only_radio.setChecked(True)
    widget.slot_checks[2].setChecked(False)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled() and not widget._readiness_pending, timeout=5_000)

    widget.primary_button.click()

    batch = controller.batch
    assert batch is not None
    assert batch.occupied_slot_numbers == (1, 3, 4)
    assert widget.step_rail.active_step == "STOCK PROGRAM"
    assert widget.setup_box.isHidden()

    press_until(qtbot, widget, controller, BatchState.QR_SCANNING)
    qtbot.waitUntil(widget.qr_input.isEnabled, timeout=5_000)

    assert widget.step_rail.active_step == "QR SCAN"
    assert "SLOT 1" in widget.qr_input.placeholderText()
    assert not widget.primary_button.isEnabled()

    for qr in ("QR-1001", "QR-1003", "QR-1004"):
        qtbot.keyClicks(widget.qr_input, qr)
        qtbot.keyClick(widget.qr_input, "\r")

    assert batch.state is BatchState.QR_BOUND
    qtbot.waitUntil(lambda: widget.primary_button.text() == "COMPLETE BATCH", timeout=5_000)

    widget.primary_button.click()
    qtbot.waitUntil(lambda: widget.primary_button.text() == "PREPARE NEXT BATCH", timeout=5_000)

    assert batch.state is BatchState.UPLOAD_PENDING
    assert widget._jig_grid.cards[2].status_label.text() == "EMPTY"
    assert widget._jig_grid.cards[1].status_label.text() == "QR BOUND"

    widget.primary_button.click()
    assert controller.batch is None
    assert widget.primary_button.text() == "START"


@pytest.mark.unit
def test_scanner_rejects_duplicate_qr_with_message(panel, qtbot):
    widget, controller, _ = panel

    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled(), timeout=5_000)
    widget.stock_only_radio.setChecked(True)
    for number in (2, 3, 4):
        widget.slot_checks[number].setChecked(False)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled() and not widget._readiness_pending, timeout=5_000)
    widget.primary_button.click()

    press_until(qtbot, widget, controller, BatchState.QR_SCANNING)
    qtbot.waitUntil(widget.qr_input.isEnabled, timeout=5_000)

    qtbot.keyClicks(widget.qr_input, "x")
    qtbot.keyClick(widget.qr_input, "\r")

    assert "Invalid QR" in widget.error_label.text()
    assert controller.batch.state is BatchState.QR_SCANNING
