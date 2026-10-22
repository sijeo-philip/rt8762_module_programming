"""Stock bench window driven end to end with simulated hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings

from stationapp.bench.mac_pool import BenchMacPool
from stationapp.bench.runner import StockBenchRunner
from stationapp.bench.simulated import SimulatedBenchHardware
from stationapp.bench.window import COL_QR, COL_RESULT, BenchContext, BenchWindow


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

    result = {row + 1: window.table.item(row, COL_RESULT).text() for row in range(4)}
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
def rig_window(qtbot, tmp_path: Path, monkeypatch):
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
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    win.close()


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
    assert window.table.item(0, COL_RESULT).text() == "RF PENDING"
    assert "slot 1, 2" in window.status_label.text()
    assert not window.rf_button.isHidden() and window.rf_button.isEnabled()
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)
    assert not window.start_button.isEnabled()                 # finish this run first
    assert window._run.csv_path is None                         # not recorded yet

    window.rf_button.click()
    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=10_000)

    assert window.table.item(0, COL_RESULT).text() == "PASS"
    assert window.table.item(1, COL_RESULT).text() == "PASS"
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

    assert window.table.item(0, COL_RESULT).text() == "PASS (no RF)"
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
    assert window.table.item(0, COL_QR).text() == "AA11BB22"
    assert window.table.item(1, COL_QR).text() == "EE55FF66"           # failed module recorded
    assert window.table.item(1, COL_RESULT).text() == "FAIL"
    assert window.table.item(2, COL_QR).text() == "CC33DD44"
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

    assert window.table.item(0, COL_QR).text() == "0000ABCD"
    assert window.table.item(1, COL_QR).text() == "NOT SCANNED"
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

    assert window.table.item(0, COL_QR).text() == "0A00000B"
    assert "0A00000B" in (window._ctx.results_dir / "qr_scan_test.csv").read_text(encoding="utf-8")

    bridge.status_changed.emit("HID POS scanner: not connected (keyboard-mode scanners still work)")
    assert "not connected" in window.scanner_status.text()


# ---------------------------------------------------------------------------
# Stock + Pricol through the operator buttons
# ---------------------------------------------------------------------------


@pytest.fixture
def pricol_window(qtbot, tmp_path: Path, monkeypatch):
    from stationapp.services.simulation import SimulatedFunctionalTester, parse_failures

    stock_fw, pricol_fw = tmp_path / "stock.bin", tmp_path / "pricol.bin"
    stock_fw.write_bytes(b"stock")
    pricol_fw.write_bytes(b"pricol")
    pool = BenchMacPool(tmp_path / "state.json")
    pool.set_range("02:5A:00:00:01:00", "02:5A:00:00:01:FF")
    pricol_pool = BenchMacPool(tmp_path / "pricol_state.json")
    pricol_pool.set_range("02:5B:00:00:01:00", "02:5B:00:00:01:FF")
    runner = StockBenchRunner(
        hardware_factory=lambda settings: SimulatedBenchHardware(delay_s=0.01),
        mac_pool=pool,
        results_dir=tmp_path / "results",
        pricol_mac_pool=pricol_pool,
        functional_tester=SimulatedFunctionalTester(delay_seconds=0.0, failures=parse_failures("app:2")),
    )
    context = BenchContext(
        runner=runner,
        mac_pool=pool,
        jig_positions=4,
        results_dir=tmp_path / "results",
        mpcli_path=tmp_path / "mpcli.exe",
        firmware=stock_fw,
        port_lister=lambda: [(f"COM10{n}", "sim") for n in range(1, 5)],
        slot_ports={n: f"COM10{n}" for n in range(1, 5)},
        pricol_mac_pool=pricol_pool,
        pricol_firmware=pricol_fw,
    )
    win = BenchWindow(context, preferences=QSettings(str(tmp_path / "bench.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    yield win
    win._operations.cancel_all()
    win._operations.wait_for_all(5_000)
    # A failed test may leave the run waiting: never block teardown on a dialog.
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    win.close()


def press_step(qtbot, window, label, next_text):
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=10_000)
    assert window.step_button.text() == label and window.step_button.isEnabled()
    window.step_button.click()
    qtbot.waitUntil(lambda: next_text in window.status_label.text() or next_text in window.qr_prompt.text(), timeout=10_000)


@pytest.mark.unit
def test_stock_and_pricol_run_is_driven_by_operator_buttons(pricol_window, qtbot):
    from stationapp.bench.window import COL_FUNCTIONAL, COL_PRICOL_MAC, COL_PRICOL_READBACK

    window = pricol_window
    window.stock_pricol_radio.setChecked(True)
    assert not window.table.isColumnHidden(COL_PRICOL_MAC)
    window.scan_qr_check.setChecked(True)
    for number in (1, 2):
        window.use_checks[number].setChecked(True)

    window.start_button.click()
    # Stock programmed (RF skipped): modules must go back to boot mode.
    qtbot.waitUntil(lambda: "SWITCH MODULES TO BOOT MODE" in window.status_label.text(), timeout=10_000)
    assert window.table.item(0, COL_PRICOL_MAC).text() == ""          # nothing flashed yet
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)
    assert not window.start_button.isEnabled()

    press_step(qtbot, window, "PROGRAM PRICOL FIRMWARE", "SWITCH MODULES TO APPLICATION MODE")
    assert window.table.item(0, COL_PRICOL_MAC).text().startswith("02:5B")
    assert window.table.item(0, COL_PRICOL_READBACK).text() == "MATCH"
    assert window.table.item(0, COL_FUNCTIONAL).text() == ""          # not tested yet

    press_step(qtbot, window, "RUN APP TEST", "READY FOR DFU TEST")
    assert window.table.item(1, COL_FUNCTIONAL).text().startswith("APP FAIL")

    press_step(qtbot, window, "RUN DFU TEST", "SCAN QR of module in SLOT 1")
    assert window.table.item(0, COL_FUNCTIONAL).text() == "APP PASS / DFU PASS"
    window.qr_input.submit("AA11BB22")
    window.qr_input.submit("CC33DD44")

    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=5_000)
    assert window.table.item(0, COL_RESULT).text() == "PASS (no RF)"
    assert window.table.item(1, COL_RESULT).text() == "FUNCTIONAL FAIL"
    assert "1/2 passed" in window.status_label.text()
    assert window.step_button.isHidden() and window.start_button.isEnabled()


@pytest.mark.unit
def test_operator_can_stop_before_pricol_programming(pricol_window, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window = pricol_window
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.stock_pricol_radio.setChecked(True)
    window.use_checks[1].setChecked(True)
    window.start_button.click()
    qtbot.waitUntil(lambda: window.stop_step_button.text() == "Stop here (no Pricol)" and not window.stop_step_button.isHidden(), timeout=10_000)
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)

    window.stop_step_button.click()

    assert window.table.item(0, COL_RESULT).text() == "PRICOL NOT RUN"
    assert window._run.csv_path.exists()
    assert window.start_button.isEnabled()


@pytest.mark.unit
def test_pricol_columns_hidden_in_stock_only_mode(pricol_window):
    from stationapp.bench.window import PRICOL_COLUMNS

    pricol_window.stock_only_radio.setChecked(True)
    assert all(pricol_window.table.isColumnHidden(column) for column in PRICOL_COLUMNS)


# ---------------------------------------------------------------------------
# RESTART WORKFLOW
# ---------------------------------------------------------------------------


def csv_rows(window):
    import csv as _csv

    path = window._ctx.results_dir / "stock_bench_results.csv"
    return list(_csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


@pytest.fixture
def yes_to_questions(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)


@pytest.mark.unit
def test_restart_from_a_waiting_step_records_the_run_and_starts_again(pricol_window, qtbot, yes_to_questions):
    window = pricol_window
    assert not window.restart_button.isEnabled()                   # nothing to restart yet
    window.stock_pricol_radio.setChecked(True)
    for number in (1, 2):
        window.use_checks[number].setChecked(True)

    window.start_button.click()
    qtbot.waitUntil(lambda: "SWITCH MODULES TO BOOT MODE" in window.status_label.text(), timeout=10_000)
    qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)
    first = window._run

    window.restart_button.click()

    qtbot.waitUntil(lambda: window._run is not first and window._run.pricol_step.value == "PROGRAM", timeout=10_000)
    assert window.restart_count == 1
    rows = csv_rows(window)
    assert [(row["run_id"], row["verdict"]) for row in rows] == [(first.run_id, "ABORTED"), (first.run_id, "ABORTED")]
    assert "workflow restarted" in rows[0]["message"]
    assert window._run.slots[1].mac != first.slots[1].mac             # MACs are never reused
    qtbot.waitUntil(lambda: "SWITCH MODULES TO BOOT MODE" in window.status_label.text(), timeout=5_000)


@pytest.mark.unit
def test_restart_while_programming_stops_it_and_starts_again(pricol_window, qtbot, yes_to_questions):
    from stationapp.bench.simulated import SimulatedBenchHardware as Hardware

    window = pricol_window
    window._ctx.runner._hardware_factory = lambda settings: Hardware(delay_s=0.5)  # slow MPCLI
    window.use_checks[1].setChecked(True)

    window.start_button.click()
    qtbot.waitUntil(lambda: window.table.item(0, 3).text().startswith("Flashing"), timeout=5_000)
    window.restart_button.click()
    assert "stopping the current step" in window.status_label.text()

    qtbot.waitUntil(lambda: window.restart_count == 1 and window._operations.active_count == 1, timeout=15_000)
    window._ctx.runner._hardware_factory = lambda settings: Hardware(delay_s=0.01)
    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=15_000)
    verdicts = [row["verdict"] for row in csv_rows(window)]
    assert verdicts[0] in {"CANCELLED", "ABORTED"} and verdicts[-1] == "PASS (no RF)"


@pytest.mark.unit
def test_restart_after_a_finished_run_repeats_the_workflow(pricol_window, qtbot, yes_to_questions):
    window = pricol_window
    window.use_checks[1].setChecked(True)
    window.start_button.click()
    qtbot.waitUntil(lambda: "passed" in window.status_label.text(), timeout=10_000)

    for repeat in (1, 2, 3):
        qtbot.waitUntil(lambda: not window._operations.active_count, timeout=5_000)
        previous = window._run
        window.restart_button.click()
        qtbot.waitUntil(lambda: window._run is not previous and "passed" in window.status_label.text(), timeout=10_000)

    assert window.restart_count == 3
    rows = csv_rows(window)
    assert len(rows) == 4 and all(row["verdict"] == "PASS (no RF)" for row in rows)
    assert len({row["mac"] for row in rows}) == 4


@pytest.mark.unit
def test_pricol_readback_column_says_why_nothing_was_read(pricol_window):
    from stationapp.bench.runner import SlotRun
    from stationapp.bench.window import COL_PRICOL_READBACK

    slot = SlotRun(1, "COM101", pricol_mac="02:5B:00:00:01:00", pricol_flash="FAILED")
    pricol_window._apply_update(slot)
    assert pricol_window.table.item(0, COL_PRICOL_READBACK).text() == "not read (Pricol flash FAILED)"

    slot = SlotRun(1, "COM101", pricol_flash="SUCCESS", pricol_mac_write="FAILED")
    pricol_window._apply_update(slot)
    assert pricol_window.table.item(0, COL_PRICOL_READBACK).text() == "not read (Pricol MAC write FAILED)"


@pytest.mark.unit
def test_chip_erase_choice_reaches_the_run(pricol_window, qtbot):
    window = pricol_window
    assert window.erase_combo.currentData() == "full"               # default: full chip erase
    seen = []
    runner = window._ctx.runner
    original = runner.run

    def run(settings, *args, **kwargs):
        seen.append((settings.chip_erase, kwargs["pricol_settings"].chip_erase if kwargs.get("pricol_settings") else None))
        return original(settings, *args, **kwargs)

    runner.run = run
    window.stock_pricol_radio.setChecked(True)
    window.use_checks[1].setChecked(True)
    window.erase_combo.setCurrentIndex(window.erase_combo.findData("keep_first_4k"))
    window.start_button.click()
    qtbot.waitUntil(lambda: bool(seen), timeout=5_000)
    assert seen[0] == ("keep_first_4k", "keep_first_4k")
    qtbot.waitUntil(lambda: "SWITCH MODULES TO BOOT MODE" in window.status_label.text(), timeout=10_000)
