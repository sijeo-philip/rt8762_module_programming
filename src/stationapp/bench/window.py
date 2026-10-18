"""Stock firmware bench tool window."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.bench.mac_pool import BenchMacPool, MacRangeError
from stationapp.bench.runner import BenchRun, BenchSettings, RfMode, SlotRun, StockBenchRunner
from stationapp.concurrency.manager import OperationManager

logger = logging.getLogger(__name__)

COLUMNS = ["Use", "Slot", "COM port", "Stage", "Progress", "MAC", "Readback", "RF", "Result"]
RESULT_COLOURS = {"PASS": "#dcfce7", "FAIL": "#fee2e2", "CANCELLED": "#fef3c7"}


@dataclass
class BenchContext:
    runner: StockBenchRunner
    mac_pool: BenchMacPool
    jig_positions: int
    results_dir: Path
    mpcli_path: Path | None = None
    firmware: Path | None = None
    golden_rig_status: str = "No approved Golden Rig"
    port_lister: object = None          # () -> list[(device, description)]
    slot_ports: dict[int, str] | None = None
    simulation: bool = False


class ManualRfDialog(QDialog):
    def __init__(self, slots: list[SlotRun], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manual RF check")
        layout = QVBoxLayout(self)
        intro = QLabel(
            "For each module, connect to the MAC below with a BLE phone app "
            "(e.g. nRF Connect), confirm the connection, then disconnect. "
            "Tick only the modules that connected and disconnected correctly."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        grid = QGridLayout()
        self.checks: dict[int, QCheckBox] = {}
        for row, slot in enumerate(slots):
            grid.addWidget(QLabel(f"Slot {slot.slot_number}"), row, 0)
            mac = QLabel(slot.mac)
            mac.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            mac.setStyleSheet("font-family: Consolas, monospace; font-size: 14px;")
            grid.addWidget(mac, row, 1)
            check = QCheckBox("Connected and disconnected OK")
            grid.addWidget(check, row, 2)
            self.checks[slot.slot_number] = check
        layout.addLayout(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @property
    def decisions(self) -> dict[int, bool]:
        return {slot: check.isChecked() for slot, check in self.checks.items()}


class BenchWindow(QMainWindow):
    _slot_update = pyqtSignal(object)

    def __init__(self, context: BenchContext, *, preferences: QSettings | None = None) -> None:
        super().__init__()
        self._ctx = context
        self._operations = OperationManager(self)
        # Per-user conveniences (paths, range, COM choices). Tests inject a
        # file-backed store so they never touch the user's registry.
        self._settings = preferences or QSettings("StationApp", "StockBench")
        self._run: BenchRun | None = None
        self._run_error: str | None = None

        self.setWindowTitle("Stock Firmware Bench" + (" - SIMULATION" if context.simulation else ""))
        self.resize(1250, 820)
        self._build()
        self._load_preferences()
        self._slot_update.connect(self._apply_update, Qt.ConnectionType.QueuedConnection)
        self._refresh_ports()
        self._refresh_mac_status()

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        if self._ctx.simulation:
            banner = QLabel("SIMULATION - no MPCLI or jig is used")
            banner.setStyleSheet("background:#92400e;color:white;font-weight:700;padding:6px;")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(banner)

        top = QHBoxLayout()
        layout.addLayout(top)

        config = QGroupBox("MPCLI and Stock firmware")
        form = QFormLayout(config)
        self.mpcli_input = self._path_row(form, "MPCLI", "mpcli.exe (*.exe);;All files (*)")
        self.firmware_input = self._path_row(form, "Stock firmware", "Firmware (*.bin);;All files (*)")
        sequence = QHBoxLayout()
        self.two_step_radio = QRadioButton("Flash, then write MAC (validated)")
        self.combined_radio = QRadioButton("Combined command")
        self.two_step_radio.setChecked(True)
        sequence.addWidget(self.two_step_radio)
        sequence.addWidget(self.combined_radio)
        form.addRow("Sequence", sequence)
        top.addWidget(config, 3)

        right = QVBoxLayout()
        macs = QGroupBox("Test MAC range")
        mac_form = QFormLayout(macs)
        self.mac_start_input = QLineEdit()
        self.mac_end_input = QLineEdit()
        mac_form.addRow("First MAC", self.mac_start_input)
        mac_form.addRow("Last MAC", self.mac_end_input)
        self.mac_status = QLabel()
        self.mac_status.setWordWrap(True)
        mac_form.addRow("", self.mac_status)
        note = QLabel("Each MAC is used once, even if programming fails.")
        note.setStyleSheet("color:#64748b;")
        mac_form.addRow("", note)
        apply_range = QPushButton("Set range")
        apply_range.clicked.connect(self._apply_range)
        mac_form.addRow("", apply_range)
        right.addWidget(macs)

        rf = QGroupBox("RF test")
        rf_layout = QVBoxLayout(rf)
        self.rf_group = QButtonGroup(self)
        self.rf_skip = QRadioButton("Skip RF")
        self.rf_rig = QRadioButton(f"Golden Rig ({self._ctx.golden_rig_status})")
        self.rf_manual = QRadioButton("Manual (operator checks with a BLE phone app)")
        self.rf_skip.setChecked(True)
        self.rf_rig.setEnabled(self._ctx.runner.golden_rig_available)
        for button in (self.rf_skip, self.rf_rig, self.rf_manual):
            self.rf_group.addButton(button)
            rf_layout.addWidget(button)
        right.addWidget(rf)
        top.addLayout(right, 2)

        self.table = QTableWidget(self._ctx.jig_positions, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.use_checks: dict[int, QCheckBox] = {}
        self.port_combos: dict[int, QComboBox] = {}
        self.progress_bars: dict[int, QProgressBar] = {}
        for row in range(self._ctx.jig_positions):
            number = row + 1
            check = QCheckBox()
            self.use_checks[number] = check
            self.table.setCellWidget(row, 0, _centred(check))
            self.table.setItem(row, 1, QTableWidgetItem(str(number)))
            combo = QComboBox()
            combo.setEditable(True)
            combo.setMinimumWidth(110)
            self.port_combos[number] = combo
            self.table.setCellWidget(row, 2, combo)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            self.progress_bars[number] = bar
            self.table.setCellWidget(row, 4, bar)
            for column in (3, 5, 6, 7, 8):
                self.table.setItem(row, column, QTableWidgetItem(""))
        self.table.cellDoubleClicked.connect(self._show_slot_log)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.rescan_button = QPushButton("Rescan COM ports")
        self.rescan_button.clicked.connect(self._refresh_ports)
        buttons.addWidget(self.rescan_button)
        self.start_button = QPushButton("START")
        self.start_button.setMinimumHeight(44)
        self.start_button.setMinimumWidth(200)
        self.start_button.setStyleSheet("QPushButton{font-size:16px;font-weight:700;color:white;background:#1d4ed8;border-radius:6px;}QPushButton:disabled{background:#cbd5e1;color:#475569;}")
        self.start_button.clicked.connect(self._start)
        buttons.addWidget(self.start_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        buttons.addWidget(self.status_label, 1)
        error_codes = QPushButton("Error codes")
        error_codes.setToolTip("What each MPCLI error number means and what to do")
        error_codes.clicked.connect(self._show_error_codes)
        buttons.addWidget(error_codes)
        open_results = QPushButton("Open results folder")
        open_results.clicked.connect(self._open_results)
        buttons.addWidget(open_results)
        layout.addLayout(buttons)

    def _path_row(self, form: QFormLayout, label: str, file_filter: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit()
        browse = QPushButton("Browse...")

        def choose() -> None:
            path, _ = QFileDialog.getOpenFileName(self, label, edit.text(), file_filter)
            if path:
                edit.setText(path)

        browse.clicked.connect(choose)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        form.addRow(label, row)
        return edit

    # ------------------------------------------------------- preferences

    def _load_preferences(self) -> None:
        ctx, prefs = self._ctx, self._settings
        self.mpcli_input.setText(prefs.value("mpcli", str(ctx.mpcli_path or "")))
        self.firmware_input.setText(prefs.value("firmware", str(ctx.firmware or "")))
        self.combined_radio.setChecked(prefs.value("combined", "false") == "true")
        if ctx.mac_pool.start is not None:
            self.mac_start_input.setText(str(ctx.mac_pool.start))
            self.mac_end_input.setText(str(ctx.mac_pool.end))
        for number, check in self.use_checks.items():
            check.setChecked(prefs.value(f"use/{number}", "false") == "true")

    def _save_preferences(self) -> None:
        prefs = self._settings
        prefs.setValue("mpcli", self.mpcli_input.text())
        prefs.setValue("firmware", self.firmware_input.text())
        prefs.setValue("combined", "true" if self.combined_radio.isChecked() else "false")
        for number, check in self.use_checks.items():
            prefs.setValue(f"use/{number}", "true" if check.isChecked() else "false")
            prefs.setValue(f"port/{number}", self.port_combos[number].currentText())

    # ------------------------------------------------------- ports / MACs

    def _refresh_ports(self) -> None:
        ports: list[tuple[str, str]] = []
        if self._ctx.port_lister is not None:
            try:
                ports = list(self._ctx.port_lister())
            except Exception as exc:  # noqa: BLE001
                self.status_label.setText(f"Port scan failed: {exc}")
        bound = self._ctx.slot_ports or {}
        for number, combo in self.port_combos.items():
            current = combo.currentText() or self._settings.value(f"port/{number}", "") or bound.get(number, "")
            combo.clear()
            for device, description in ports:
                combo.addItem(device, device)
                combo.setItemData(combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentText(current)

    def _apply_range(self) -> None:
        try:
            self._ctx.mac_pool.set_range(self.mac_start_input.text(), self.mac_end_input.text())
        except (MacRangeError, ValueError) as exc:
            QMessageBox.warning(self, "Test MAC range", str(exc))
        self._refresh_mac_status()

    def _refresh_mac_status(self) -> None:
        pool = self._ctx.mac_pool
        nxt = pool.peek_next()
        if pool.start is None:
            self.mac_status.setText("No range set")
            self.mac_status.setStyleSheet("color:#991b1b;")
        else:
            self.mac_status.setText(f"Next: {nxt or '-'} | {pool.remaining()} unused")
            self.mac_status.setStyleSheet("color:#166534;" if pool.remaining() else "color:#991b1b;")

    # ------------------------------------------------------------- run

    def _selected_slots(self) -> dict[int, str]:
        return {
            number: self.port_combos[number].currentText().strip()
            for number, check in self.use_checks.items()
            if check.isChecked()
        }

    def _rf_mode(self) -> RfMode:
        if self.rf_rig.isChecked():
            return RfMode.GOLDEN_RIG
        if self.rf_manual.isChecked():
            return RfMode.MANUAL
        return RfMode.SKIP

    def _start(self) -> None:
        slots = self._selected_slots()
        if not slots:
            self.status_label.setText("Tick at least one slot")
            return
        missing = [str(number) for number, port in slots.items() if not port]
        if missing:
            self.status_label.setText("Choose a COM port for slot(s) " + ", ".join(missing))
            return

        settings = BenchSettings(
            mpcli_path=Path(self.mpcli_input.text().strip()),
            firmware=Path(self.firmware_input.text().strip()),
            two_step=self.two_step_radio.isChecked(),
        )
        mode = self._rf_mode()
        self._save_preferences()
        self._clear_rows(slots)
        runner = self._ctx.runner
        self._run_error = None

        def operation(_id, token, _progress):
            try:
                return runner.run(settings, slots, mode, on_update=self._slot_update.emit, token=token)
            except (ValueError, MacRangeError) as exc:
                return exc

        def configure(worker) -> None:
            worker.succeeded.connect(lambda result: self._on_done(result.value))
            worker.failed.connect(lambda failure: self._on_failed(failure.user_message))
            worker.cancelled.connect(lambda _id: self.status_label.setText("Cancelled"))
            worker.finished.connect(lambda _id: self._set_running(False))

        try:
            self._operations.start(operation, operation_id="stock-bench", configure=configure)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not start: {exc}")
            return
        self._set_running(True)
        self.status_label.setText(f"Running {len(slots)} slot(s)...")

    def _cancel(self) -> None:
        self._operations.cancel("stock-bench")
        self.status_label.setText("Cancelling - waiting for MPCLI to stop...")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.rescan_button.setEnabled(not running)
        for check in self.use_checks.values():
            check.setEnabled(not running)
        for combo in self.port_combos.values():
            combo.setEnabled(not running)
        self._refresh_mac_status()

    def _on_failed(self, message: str) -> None:
        self.status_label.setText(f"Run failed: {message}")

    def _on_done(self, value) -> None:
        if isinstance(value, Exception):
            self.status_label.setText(str(value))
            QMessageBox.warning(self, "Cannot start", str(value))
            return
        run: BenchRun = value
        self._run = run
        if run.awaiting_manual_rf:
            dialog = ManualRfDialog(run.awaiting_manual_rf, self)
            dialog.exec()
            run = self._ctx.runner.complete_manual_rf(run, dialog.decisions, on_update=self._apply_update)
            self._run = run
        passed = sum(1 for slot in run.slots.values() if slot.verdict.startswith("PASS"))
        self.status_label.setText(
            f"Run {run.run_id}: {passed}/{len(run.slots)} passed. Results: {run.csv_path} "
            "(double-click a row for its MPCLI log)"
        )

    def _clear_rows(self, slots: dict[int, str]) -> None:
        for row in range(self.table.rowCount()):
            number = row + 1
            for column in (3, 5, 6, 7, 8):
                item = self.table.item(row, column)
                item.setText("" if number in slots else "")
                item.setBackground(QColor("white"))
            self.progress_bars[number].setValue(0)

    def _apply_update(self, slot: SlotRun) -> None:
        row = slot.slot_number - 1
        values = {
            3: slot.stage + (f" - {slot.message}" if slot.message else ""),
            5: slot.mac,
            6: slot.readback + (f" ({slot.readback_mac})" if slot.readback_mac and slot.readback != "MATCH" else ""),
            7: slot.rf + (f" - {slot.rf_detail}" if slot.rf_detail and slot.rf not in {"-", "SKIPPED"} else ""),
            8: slot.verdict,
        }
        for column, text in values.items():
            item = self.table.item(row, column)
            item.setText(text)
            item.setToolTip(text)
        colour = next((value for key, value in RESULT_COLOURS.items() if slot.verdict.startswith(key)), None)
        if slot.verdict and colour is None:
            colour = "#fef3c7"
        if colour:
            self.table.item(row, 8).setBackground(QColor(colour))
        self.progress_bars[slot.slot_number].setValue(slot.progress)

    def _show_slot_log(self, row: int, _column: int) -> None:
        run = self._run
        if run is None or run.log_dir is None:
            return
        slot = run.slots.get(row + 1)
        if slot is None:
            return
        path = run.log_dir / f"slot{slot.slot_number}_{slot.com_port}.log"
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_error_codes(self) -> None:
        from stationapp.ui.dialogs.error_codes_dialog import ErrorCodesDialog

        ErrorCodesDialog(self, highlight=self._last_error_code()).exec()

    def _last_error_code(self) -> int | None:
        """First MPCLI error number in the current run, to highlight it."""
        import re

        if self._run is None:
            return None
        for slot in self._run.slots.values():
            match = re.search(r"MPCLI ERR (\d+)", slot.message or "")
            if match:
                return int(match.group(1))
        return None

    def _open_results(self) -> None:
        self._ctx.results_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._ctx.results_dir)))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._operations.active_count:
            if QMessageBox.question(self, "Run in progress", "Cancel the run and close?") is not QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._operations.cancel_all()
            self._operations.wait_for_all(20_000)
        self._save_preferences()
        event.accept()


def _centred(widget: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(widget)
    return container
