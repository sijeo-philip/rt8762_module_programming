"""Bind from reference modules: Station configuration page and bench."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QMessageBox

from stationapp.concurrency.manager import OperationManager
from stationapp.ui.dialogs import reference_binding_dialog
from stationapp.ui.widgets import station_configuration as screen
from tests.integration.services.test_reference_slot_binding import station  # noqa: F401 - fixture


@pytest.fixture
def approve_everything(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(screen, "ask_reason", lambda *args, **kwargs: "commissioning")
    shown = []

    def exec_(dialog):
        shown.append(dialog)
        return True

    monkeypatch.setattr(reference_binding_dialog.ReferenceBindingDialog, "exec", exec_)
    return shown


@pytest.mark.unit
def test_supervisor_binds_slots_from_reference_modules(qtbot, station, approve_everything):  # noqa: F811
    station.access.login_supervisor("sup1", "supervisor-pw")
    operations = OperationManager()
    page = screen.SlotBindingPage(station.jig, 4, simulation=False, operations=operations)
    qtbot.addWidget(page)
    page.refresh()
    assert page.reference_button.isEnabled()

    page.reference_button.click()
    qtbot.waitUntil(lambda: bool(approve_everything) and page.binding_table.item(0, 1).text() != "unbound", timeout=5_000)

    dialog = approve_everything[0]
    assert dialog.table.item(0, 1).text() == "COM6"               # slot 1 <- COM6 (22:..:01)
    assert dialog.table.item(0, 4).text() == "new binding"
    assert page.binding_table.item(0, 1).text() == "USB:0403:6001:SERIAL:BBB"
    assert page.binding_table.item(2, 1).text() == "USB:0403:6001:SERIAL:AAA"
    operations.wait_for_all(5_000)


@pytest.mark.unit
def test_button_disabled_without_mpcli(qtbot, station):  # noqa: F811
    from stationapp.services.jig_configuration import JigConfigurationService

    jig = JigConfigurationService(
        station_id="STATION-01", binding_service=station.bindings, access=station.access,
        session_factory=station.factory, discover=lambda: (),
    )
    page = screen.SlotBindingPage(jig, 4, simulation=True, operations=OperationManager())
    qtbot.addWidget(page)
    assert not page.reference_button.isEnabled()


@pytest.mark.unit
def test_bench_auto_assigns_com_ports(qtbot, tmp_path: Path, approve_everything):
    from stationapp.bench.mac_pool import BenchMacPool
    from stationapp.bench.runner import StockBenchRunner
    from stationapp.bench.simulated import SimulatedBenchHardware
    from stationapp.bench.window import BenchContext, BenchWindow

    # COM101 holds slot 2's reference module, COM102 slot 3's, ...
    reference = {f"COM10{n}": f"22:22:22:22:22:{n % 4 + 1:02X}" for n in range(1, 5)}
    pool = BenchMacPool(tmp_path / "state.json")
    runner = StockBenchRunner(
        hardware_factory=lambda settings: SimulatedBenchHardware(delay_s=0.01, reference_macs=reference),
        mac_pool=pool,
        results_dir=tmp_path / "results",
    )
    context = BenchContext(
        runner=runner, mac_pool=pool, jig_positions=4, results_dir=tmp_path / "results",
        port_lister=lambda: [(f"COM10{n}", "sim") for n in range(1, 5)],
    )
    window = BenchWindow(context, preferences=QSettings(str(tmp_path / "bench.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(window)
    for check in window.use_checks.values():
        check.setChecked(False)

    window.autobind_button.click()
    qtbot.waitUntil(lambda: "Auto-assigned" in window.status_label.text(), timeout=10_000)

    assert {n: window.port_combos[n].currentText() for n in range(1, 5)} == {1: "COM104", 2: "COM101", 3: "COM102", 4: "COM103"}
    assert all(check.isChecked() for check in window.use_checks.values())
    assert approve_everything[0].apply_button.text() == "Use these COM ports"
    window._operations.wait_for_all(5_000)
