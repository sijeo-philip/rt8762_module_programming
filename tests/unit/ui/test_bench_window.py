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


# ---------------------------------------------------------------------------
# Golden Rig connection check
# ---------------------------------------------------------------------------


def rig_result(ok: bool):
    from stationapp.services.golden_rig_check import RigCheckResult, RigCheckStatus, RigCheckStep

    steps = [RigCheckStep("Name lookup", RigCheckStatus.PASS, "golden-rig-01.local -> 192.168.1.50 (via mDNS)")]
    if ok:
        steps.append(RigCheckStep("Connection", RigCheckStatus.PASS, "TCP port 8762 open at 192.168.1.50"))
    else:
        steps.append(RigCheckStep("Connection", RigCheckStatus.FAIL, "No answer on TCP port 8762"))
    return RigCheckResult("golden-rig-01.local:8762", tuple(steps))


@pytest.fixture
def rig_window(qtbot, tmp_path: Path):
    from stationapp.services.simulation import SimulatedGoldenRig

    firmware = tmp_path / "stock.bin"
    firmware.write_bytes(b"fw")
    pool = BenchMacPool(tmp_path / "state.json")
    pool.set_range("02:5A:00:00:01:00", "02:5A:00:00:01:FF")
    runner = StockBenchRunner(
        hardware_factory=lambda settings: SimulatedBenchHardware(delay_s=0.01),
        mac_pool=pool,
        results_dir=tmp_path / "results",
        rf_tester=SimulatedGoldenRig(delay_seconds=0.0, failures=frozenset()),
    )
    state = {"ok": True, "calls": []}

    def rig_check(host, port):
        state["calls"].append((host, port))
        return rig_result(state["ok"])

    context = BenchContext(
        runner=runner,
        mac_pool=pool,
        jig_positions=4,
        results_dir=tmp_path / "results",
        mpcli_path=tmp_path / "mpcli.exe",
        firmware=firmware,
        port_lister=lambda: [(f"COM10{n}", "sim") for n in range(1, 5)],
        slot_ports={n: f"COM10{n}" for n in range(1, 5)},
        golden_rig_status="GOLDEN-RIG-01 at golden-rig-01.local:8762",
        rig_check=rig_check,
        rig_address=("golden-rig-01.local", 8762),
    )
    win = BenchWindow(context, preferences=QSettings(str(tmp_path / "bench.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    yield win, pool, state
    win._operations.cancel_all()
    win._operations.wait_for_all(5_000)


@pytest.mark.unit
def test_check_connection_button_shows_each_step(rig_window, qtbot):
    window, _pool, state = rig_window
    assert window.rig_host_input.text() == "golden-rig-01.local"     # the approved rig
    window.rig_host_input.setText("192.168.1.50")
    window.rig_check_button.click()
    qtbot.waitUntil(lambda: "connected" in window.rig_check_label.text(), timeout=5_000)
    assert state["calls"] == [("192.168.1.50", 8762)]
    assert "via mDNS" in window.rig_check_label.text()
    assert window.rig_check_button.isEnabled()


@pytest.mark.unit
def test_golden_rig_run_is_not_started_when_rig_check_fails(rig_window, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, pool, state = rig_window
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    state["ok"] = False
    before = pool.remaining()
    window.use_checks[1].setChecked(True)
    window.rf_rig.setChecked(True)

    window.start_button.click()
    qtbot.waitUntil(lambda: bool(warnings), timeout=5_000)

    assert state["calls"] == [("golden-rig-01.local", 8762)]        # checked the approved address
    assert "no MACs were used" in warnings[0] and "Connection" in warnings[0]
    assert pool.remaining() == before
    assert "NOT usable" in window.rig_check_label.text()
    qtbot.waitUntil(window.start_button.isEnabled, timeout=5_000)


@pytest.mark.unit
def test_rf_test_waits_for_operator_reset_then_runs_on_button(rig_window, qtbot):
    window, _pool, state = rig_window
    window.use_checks[1].setChecked(True)
    window.use_checks[2].setChecked(True)
    window.rf_rig.setChecked(True)

    window.start_button.click()
    qtbot.waitUntil(lambda: "RESET MODULES FOR RF TEST" in window.status_label.text(), timeout=10_000)

    # Programmed, RF not started: the operator must reset the modules first.
    assert state["calls"] == [("golden-rig-01.local", 8762)]
    assert window.table.item(0, 7).text() == "AWAITING RESET"
    assert window.table.item(0, 8).text() == "RF PENDING"
    assert "slot 1, 2" in window.status_label.text()
    assert not window.rf_button.isHidden() and window.rf_button.isEnabled()
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)
    assert not window.start_button.isEnabled()                 # finish this run first
    assert window._run.csv_path is None                         # not recorded yet

    window.rf_button.click()
    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=10_000)

    assert window.table.item(0, 8).text() == "PASS"
    assert window.table.item(1, 8).text() == "PASS"
    assert window._run.csv_path.exists()
    assert window.rf_button.isHidden()
    assert window.start_button.isEnabled()


@pytest.mark.unit
def test_finish_without_rf_records_modules_as_not_tested(rig_window, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, _pool, _state = rig_window
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.use_checks[1].setChecked(True)
    window.rf_rig.setChecked(True)
    window.start_button.click()
    qtbot.waitUntil(lambda: not window.skip_rf_button.isHidden(), timeout=10_000)
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)

    window.skip_rf_button.click()

    assert window.table.item(0, 8).text() == "PASS (no RF)"
    assert window._run.csv_path.exists()
    assert window.start_button.isEnabled()


@pytest.mark.unit
def test_skip_rf_run_does_not_check_the_rig(rig_window, qtbot):
    window, _pool, state = rig_window
    window.use_checks[1].setChecked(True)
    window.start_button.click()
    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=10_000)
    assert state["calls"] == []


# ---------------------------------------------------------------------------
# QR code scanner
# ---------------------------------------------------------------------------


def scan(qtbot, window, text):
    """A keyboard-wedge scanner burst, as the POS HID Barcode Scanner types it."""
    qtbot.keyClicks(window.qr_input, text)
    qtbot.wait(150)                     # burst settles -> scan complete


@pytest.mark.unit
def test_idle_scan_shows_scanner_works(window, qtbot):
    assert "Scanner ready" in window.qr_prompt.text()
    scan(qtbot, window, "PRICOL/1a2b3c4d")
    assert "Scanner OK - read 1A2B3C4D" in window.qr_prompt.text()
    qtbot.keyClicks(window.qr_input, "ABC")                    # too short
    window.qr_input.returnPressed.emit()
    assert "Not a valid QR" in window.qr_prompt.text()


@pytest.mark.unit
def test_qrs_are_scanned_against_each_passed_slot_after_the_run(window, qtbot):
    for number in (1, 2, 3):
        window.use_checks[number].setChecked(True)
    window.use_checks[4].setChecked(False)
    window.scan_qr_check.setChecked(True)

    window.start_button.click()
    qtbot.waitUntil(lambda: "SLOT 1" in window.qr_prompt.text(), timeout=10_000)
    assert window._run.csv_path is None                         # recorded after QRs
    assert window.table.item(0, 3).text() == "Scan QR"
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)
    assert not window.start_button.isEnabled()

    scan(qtbot, window, "AA11BB22")
    # Slot 2 failed programming (COM102): its QR is still scanned.
    assert "SLOT 2 (FAIL" in window.qr_prompt.text()
    scan(qtbot, window, "AA11BB22")                             # repeat read: ignored
    assert "read twice" in window.qr_prompt.text()
    window.qr_input.forget_last_scan()
    scan(qtbot, window, "AA11BB22")                             # same label on slot 2
    assert "already scanned for slot 1" in window.qr_prompt.text()
    scan(qtbot, window, "EE55FF66")
    assert "SLOT 3" in window.qr_prompt.text()
    scan(qtbot, window, "CC33DD44")

    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=5_000)
    assert window.table.item(0, 9).text() == "AA11BB22"
    assert window.table.item(1, 9).text() == "EE55FF66"           # failed module recorded
    assert window.table.item(1, 8).text() == "FAIL"
    assert window.table.item(2, 9).text() == "CC33DD44"
    csv_text = window._run.csv_path.read_text(encoding="utf-8")
    assert "AA11BB22" in csv_text and "CC33DD44" in csv_text and ",qr,qr_note," in csv_text
    assert window.start_button.isEnabled()
    assert window.skip_qr_button.isHidden()


@pytest.mark.unit
def test_scanner_test_without_programming(window, qtbot):
    window.use_checks[1].setChecked(True)
    window.use_checks[2].setChecked(True)
    window.use_checks[3].setChecked(False)
    window.use_checks[4].setChecked(False)

    window.qr_test_button.click()
    assert "SCANNER TEST" in window.qr_prompt.text() and "SLOT 1" in window.qr_prompt.text()
    assert not window.start_button.isEnabled()
    scan(qtbot, window, "0000ABCD")
    window.skip_qr_button.click()                               # slot 2 skipped

    assert window.table.item(0, 9).text() == "0000ABCD"
    assert window.table.item(1, 9).text() == "NOT SCANNED"
    assert "1 QR(s) read" in window.status_label.text()
    saved = (window._ctx.results_dir / "qr_scan_test.csv").read_text(encoding="utf-8")
    assert "0000ABCD" in saved
    assert window.start_button.isEnabled()
    assert window.table.item(0, 5).text() == ""                 # nothing programmed


@pytest.mark.unit
def test_hid_pos_scanner_reads_fill_the_qr_column(window, qtbot):
    from tests.unit.drivers.test_pos_scanner import FakeBridge

    bridge = FakeBridge()
    window.attach_pos_scanner(bridge)
    assert "1470g connected" in window.scanner_status.text()

    window.use_checks[1].setChecked(True)
    for number in (2, 3, 4):
        window.use_checks[number].setChecked(False)
    window.qr_test_button.click()
    bridge.scanned.emit("Unisem0A00000B")      # no keystrokes: HID POS mode

    assert window.table.item(0, 9).text() == "0A00000B"
    assert "0A00000B" in (window._ctx.results_dir / "qr_scan_test.csv").read_text(encoding="utf-8")

    bridge.status_changed.emit("HID POS scanner: not connected (keyboard-mode scanners still work)")
    assert "not connected" in window.scanner_status.text()
