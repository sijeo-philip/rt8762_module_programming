"""Stock bench window driven end to end with simulated hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings

from stationapp.bench.mac_pool import BenchMacPool
from stationapp.bench.runner import StockBenchRunner
from stationapp.bench.simulated import SimulatedBenchHardware
from stationapp.bench.window import BenchContext, BenchWindow


@pytest.fixture
def window(qtbot, tmp_path: Path, monkeypatch):
    firmware = tmp_path / "stock.bin"
    firmware.write_bytes(b"fw")
    pool = BenchMacPool(tmp_path / "state.json")
    pool.set_range("02:5A:00:00:01:00", "02:5A:00:00:01:FF")
    runner = StockBenchRunner(
        hardware_factory=lambda settings: SimulatedBenchHardware(delay_s=0.01, fail_ports=frozenset({"COM102"})),
        mac_pool=pool,
        results_dir=tmp_path / "results",
    )
    context = BenchContext(
        runner=runner,
        mac_pool=pool,
        jig_positions=4,
        results_dir=tmp_path / "results",
        mpcli_path=tmp_path / "mpcli.exe",
        firmware=firmware,
        port_lister=lambda: [(f"COM10{n}", "sim") for n in range(1, 5)],
        slot_ports={n: f"COM10{n}" for n in range(1, 5)},
        simulation=True,
    )
    win = BenchWindow(context, preferences=QSettings(str(tmp_path / "bench.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    yield win
    win._operations.cancel_all()
    win._operations.wait_for_all(5_000)


@pytest.mark.unit
def test_start_requires_a_selected_slot(window):
    for check in window.use_checks.values():
        check.setChecked(False)
    window.start_button.click()
    assert "Tick at least one slot" in window.status_label.text()


@pytest.mark.unit
def test_bench_run_shows_per_slot_results(window, qtbot):
    for number in (1, 2, 3):
        window.use_checks[number].setChecked(True)
    window.use_checks[4].setChecked(False)
    assert window.port_combos[2].currentText() == "COM102"

    window.start_button.click()
    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=10_000)

    result = {row + 1: window.table.item(row, 8).text() for row in range(4)}
    assert result[1] == "PASS (no RF)"
    assert result[2] == "FAIL"
    assert result[3] == "PASS (no RF)"
    assert result[4] == ""
    assert window.table.item(0, 5).text() == "02:5A:00:00:01:00"
    assert "2/3 passed" in window.status_label.text()
    assert window._run.csv_path.exists()
    assert window.start_button.isEnabled()


@pytest.mark.unit
def test_error_codes_dialog_lists_all_codes(qtbot):
    from stationapp.ui.dialogs.error_codes_dialog import ErrorCodesDialog

    dialog = ErrorCodesDialog(highlight=6)
    qtbot.addWidget(dialog)

    assert dialog.table.rowCount() == 19
    assert dialog.table.item(6, 0).text() == "6"
    assert dialog.table.item(6, 1).text() == "hci event error"
    assert dialog.table.selectionModel().selectedRows()[0].row() == 6
