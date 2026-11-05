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
from stationapp.domain.mac import MacPurpose
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
from PyQt6.QtWidgets import QMessageBox


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
    # Blocks for both modes, so switching mode never waits for a top-up.
    mac_cache.ensure(MacPurpose.STOCK_RF_TEST)
    mac_cache.ensure(MacPurpose.PRICOL_PRODUCTION)
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
    widget.shutdown()
    operations.cancel_all()
    operations.wait_for_all(5_000)
    engine.dispose()


def check_after_setup_change(qtbot, widget) -> None:
    """Run the READY check for the new mode/slots now, not after the delay."""
    widget._readiness_timer.stop()
    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled() and not widget._readiness_pending, timeout=5_000)


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
    qtbot.waitUntil(lambda: widget.headline_label.text() == "JIG READY - PRESS START", timeout=5_000)

    assert widget.selected_slots == (1, 2, 3, 4)
    assert widget.primary_button.isEnabled()


@pytest.mark.unit
def test_operator_runs_stock_only_batch_end_to_end(panel, qtbot):
    widget, controller, _ = panel

    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled(), timeout=5_000)

    widget.stock_only_radio.setChecked(True)
    widget.slot_checks[2].setChecked(False)
    check_after_setup_change(qtbot, widget)

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

    for qr in ("1A2B1001", "1A2B1001", "1A2B1003", "1A2B1004"):   # slot 1 read twice
        qtbot.keyClicks(widget.qr_input, qr)
        qtbot.keyClick(widget.qr_input, "\r")
        # The double read is discarded silently: no "Duplicate QR" error.
        assert widget.error_label.text() == ""

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
    check_after_setup_change(qtbot, widget)
    widget.primary_button.click()

    press_until(qtbot, widget, controller, BatchState.QR_SCANNING)
    qtbot.waitUntil(widget.qr_input.isEnabled, timeout=5_000)

    qtbot.keyClicks(widget.qr_input, "AB12")
    qtbot.keyClick(widget.qr_input, "\r")

    assert "Invalid QR" in widget.error_label.text()
    assert controller.batch.state is BatchState.QR_SCANNING

    # The refusal must survive background re-renders...
    widget.render()
    assert "Invalid QR" in widget.error_label.text()

    # ...and clear after the next good scan.
    qtbot.keyClicks(widget.qr_input, "C0FFEE01")
    qtbot.keyClick(widget.qr_input, "\r")
    assert widget.error_label.text() == ""


def start_one_slot_stock_only(widget, controller, qtbot) -> None:
    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget.primary_button.isEnabled(), timeout=5_000)
    widget.stock_only_radio.setChecked(True)
    for number in (2, 3, 4):
        widget.slot_checks[number].setChecked(False)
    check_after_setup_change(qtbot, widget)
    widget.primary_button.click()


@pytest.mark.unit
def test_restart_tray_during_qr_scanning_keeps_the_mac(panel, qtbot, monkeypatch):
    widget, controller, _ = panel
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    start_one_slot_stock_only(widget, controller, qtbot)
    batch = controller.batch
    mac = batch.slots[1].stock_mac
    assert not widget.restart_button.isHidden()

    press_until(qtbot, widget, controller, BatchState.QR_SCANNING)
    qtbot.waitUntil(widget.qr_input.isEnabled, timeout=5_000)
    widget.restart_button.click()

    assert batch.state is BatchState.STOCK_MACS_RESERVED
    assert batch.slots[1].stock_mac == mac
    assert "restarted 1x" in widget.instruction_label.text()

    press_until(qtbot, widget, controller, BatchState.QR_SCANNING)
    qtbot.waitUntil(widget.qr_input.isEnabled, timeout=5_000)
    qtbot.keyClicks(widget.qr_input, "1A2B1001")
    qtbot.keyClick(widget.qr_input, "\r")
    assert batch.state is BatchState.QR_BOUND


@pytest.mark.unit
def test_restart_tray_stops_the_running_step_first(panel, qtbot, monkeypatch):
    widget, controller, operations = panel
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    start_one_slot_stock_only(widget, controller, qtbot)
    batch = controller.batch
    controller._programmer = SimulatedProgrammer(delay_seconds=5, failures=frozenset())

    press_until(qtbot, widget, controller, BatchState.AWAITING_STOCK_PROGRAM_MODE)
    qtbot.waitUntil(widget.primary_button.isEnabled, timeout=5_000)
    widget.primary_button.click()                    # programming runs for 5 s
    qtbot.waitUntil(lambda: controller.snapshot().running, timeout=5_000)
    widget.restart_button.click()
    assert widget.headline_label.text() == "STOPPING..."

    qtbot.waitUntil(lambda: batch.restart_count == 1, timeout=10_000)
    assert batch.state is BatchState.STOCK_MACS_RESERVED
    assert operations.get("production-advance") is None
    assert widget.restart_button.isEnabled()


class _Access:
    """Just enough of StationAccessService for the Batch Code strip."""

    def __init__(self) -> None:
        self.session = type("S", (), {"session_id": "SESSION-1"})()
        self.operator = None
        self.supervisor = None


class _Codes:
    def __init__(self, state) -> None:
        self.state = state

    def current(self):
        return self.state

    def problem(self, _session_id=None):
        return None if self.state.ready else "Enter the Batch Code starting series before the first tray."


@pytest.mark.unit
def test_start_waits_for_the_batch_code_series(panel, qtbot):
    from stationapp.domain.batch_code import BatchCodeState

    widget, _controller, _ = panel
    codes = _Codes(BatchCodeState(session_id="SESSION-1", threshold=100))
    widget._access, widget._batch_codes = _Access(), codes
    widget._readiness_timer.stop()
    widget.check_station(top_up=True)
    qtbot.waitUntil(lambda: widget._report is not None and not widget._readiness_pending, timeout=5_000)
    widget.render()

    assert widget.headline_label.text() == "ENTER BATCH CODE"
    assert not widget.primary_button.isEnabled()
    assert widget.batch_code_button.text() == "ENTER BATCH CODE" and widget.batch_code_button.isEnabled()

    codes.state = codes.state.started("S5A01")
    widget.render()
    assert widget.primary_button.isEnabled()
    assert "S5A01" in widget.batch_code_label.text() and "0 of 100" in widget.batch_code_label.text()
    assert widget.batch_code_button.text() == "Change Batch Code (Supervisor)"
