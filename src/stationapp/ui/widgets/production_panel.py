"""Guided operator production panel.

Renders ProductionController snapshots and forwards operator actions.
The panel holds no workflow rules: which button is enabled, what it
says, and what happens next all come from the controller and the
WorkflowService projection. The controller re-validates every command.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.concurrency.manager import OperationManager
from stationapp.domain.batch import BatchState, ProgrammingMode
from stationapp.services.health import Severity
from stationapp.services.production import ProductionController, ProductionError, ProductionSnapshot
from stationapp.services.readiness import ReadinessReport, StationReadinessService
from stationapp.services.access import StationAccessService
from stationapp.ui.dialogs.access_dialogs import SupervisorAuthorizationDialog
from stationapp.services.jig_status_projection import SlotView, SlotVisualStatus
from stationapp.drivers.qr_scanner import MODULE_QR_FORMAT
from stationapp.ui.widgets.jig_slot_grid import JigSlotGrid
from stationapp.ui.widgets.scanner_input import ScannerLineEdit

logger = logging.getLogger(__name__)

ADVANCE_OPERATION = "production-advance"
READINESS_OPERATION = "station-readiness"

BLUE = "#1d4ed8"
GREEN = "#166534"
RED = "#991b1b"
AMBER = "#92400e"
GREY = "#64748b"

_SEVERITY = {Severity.OK: GREEN, Severity.WARN: AMBER, Severity.FAIL: RED}

STEPS = (
    "LOAD & READY",
    "STOCK PROGRAM",
    "STOCK READBACK",
    "RF TEST",
    "PRICOL PROGRAM",
    "PRICOL READBACK",
    "FUNCTIONAL TEST",
    "QR SCAN",
    "FINAL VALIDATION",
    "UPLOAD QUEUED",
)
PRICOL_STEPS = frozenset({"PRICOL PROGRAM", "PRICOL READBACK", "FUNCTIONAL TEST"})

_STEP_FOR_STATE = {
    BatchState.CREATED: "LOAD & READY",
    BatchState.PORT_BINDING: "LOAD & READY",
    BatchState.PORTS_BOUND: "LOAD & READY",
    BatchState.STOCK_MACS_RESERVED: "STOCK PROGRAM",
    BatchState.AWAITING_STOCK_PROGRAM_MODE: "STOCK PROGRAM",
    BatchState.STOCK_PROGRAMMING: "STOCK PROGRAM",
    BatchState.STOCK_PROGRAMMED: "STOCK READBACK",
    BatchState.STOCK_READBACK_VERIFIED: "RF TEST",
    BatchState.AWAITING_STOCK_RF_MODE: "RF TEST",
    BatchState.STOCK_RF_TESTING: "RF TEST",
    BatchState.STOCK_RF_CONFIRMED: "RF TEST",
    BatchState.PRICOL_MACS_RESERVED: "PRICOL PROGRAM",
    BatchState.AWAITING_PRICOL_PROGRAM_MODE: "PRICOL PROGRAM",
    BatchState.PRICOL_PROGRAMMING: "PRICOL PROGRAM",
    BatchState.PRICOL_PROGRAMMED: "PRICOL READBACK",
    BatchState.PRICOL_READBACK_VERIFIED: "FUNCTIONAL TEST",
    BatchState.AWAITING_FUNCTIONAL_TEST_MODE: "FUNCTIONAL TEST",
    BatchState.AWAITING_PRICOLAPP_RESET: "FUNCTIONAL TEST",
    BatchState.PRICOLAPP_TESTING: "FUNCTIONAL TEST",
    BatchState.AWAITING_PRICOLDFU_RESET: "FUNCTIONAL TEST",
    BatchState.PRICOLDFU_TESTING: "FUNCTIONAL TEST",
    BatchState.FUNCTIONAL_TEST_COMPLETE: "FUNCTIONAL TEST",
    BatchState.QR_SCANNING: "QR SCAN",
    BatchState.QR_BOUND: "FINAL VALIDATION",
    BatchState.COMMITTED: "UPLOAD QUEUED",
    BatchState.UPLOAD_PENDING: "UPLOAD QUEUED",
    BatchState.UPLOADED: "UPLOAD QUEUED",
}


class StepRail(QWidget):
    """Horizontal workflow indicator highlighting the active step."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.labels: dict[str, QLabel] = {}
        for step in STEPS:
            label = QLabel(step)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setMinimumHeight(40)
            layout.addWidget(label, 1)
            self.labels[step] = label
        self._last_step = STEPS[0]
        self.set_step(STEPS[0], stock_only=False, blocked=False)

    def set_step(self, active: str, *, stock_only: bool, blocked: bool) -> None:
        self._last_step = active
        active_index = STEPS.index(active)
        for index, step in enumerate(STEPS):
            label = self.labels[step]
            skipped = stock_only and step in PRICOL_STEPS
            if skipped:
                colour, background, weight, text = GREY, "#f1f5f9", 400, f"{step}\n(skipped)"
            elif index < active_index:
                colour, background, weight, text = GREEN, "#dcfce7", 600, f"✓ {step}"
            elif index == active_index:
                colour = "white"
                background = RED if blocked else BLUE
                weight, text = 700, f"▶ {step}"
            else:
                colour, background, weight, text = GREY, "#f1f5f9", 400, step
            label.setText(text)
            label.setStyleSheet(
                f"color: {colour}; background-color: {background}; font-weight: {weight};"
                "border-radius: 6px; padding: 4px; font-size: 11px;"
            )
        self.setProperty("activeStep", active)

    @property
    def active_step(self) -> str:
        return self._last_step


class ProductionPanel(QWidget):
    """Operator-facing production workflow."""

    _controller_changed = pyqtSignal()
    _access_changed = pyqtSignal()
    _settings_changed = pyqtSignal()

    def __init__(self, *, controller: ProductionController, readiness: StationReadinessService, operations: OperationManager, jig_grid: JigSlotGrid, station_id: str, default_stock_firmware: Path | None = None, default_pricol_firmware: Path | None = None, simulation: bool = False, access: StationAccessService | None = None, settings_service=None, scan_debounce_ms: int = 2000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._readiness = readiness
        self._operations = operations
        self._jig_grid = jig_grid
        self._station_id = station_id
        self._simulation = simulation
        self._report: ReadinessReport | None = None
        self._readiness_pending = False
        self._readiness_rerun = False
        self._render_pending = False
        self._snapshot: ProductionSnapshot | None = None
        # Refusal of an operator action (e.g. a duplicate QR) stays visible
        # until the next successful action or workflow step.
        self._local_error: str | None = None
        self._error_state: object = None
        # Supervisor view shows batch setup and the detailed READY checks;
        # the Operator view shows only the jig, the instruction and START.
        self._supervisor_view = True
        self._shut_down = False
        self._scan_debounce_ms = scan_debounce_ms
        self._settings_service = settings_service
        self._browse_buttons: list[QPushButton] = []

        self._build(default_stock_firmware, default_pricol_firmware)

        # Controller listeners fire on worker threads; a queued signal
        # brings the update onto the GUI thread.
        self._controller_changed.connect(self._schedule_render, Qt.ConnectionType.QueuedConnection)
        controller.add_listener(self._controller_changed.emit)

        # A login, logout or session change alters READY: re-check it.
        self._access = access
        if access is not None:
            access.add_listener(self._access_changed.emit)
            self._access_changed.connect(self._on_access_changed, Qt.ConnectionType.QueuedConnection)
            self.operator_input.setReadOnly(True)
            self.operator_input.setPlaceholderText("Log in as Operator (top bar)")
        # Mode, firmware, lot and enabled slots belong to the Supervisor.
        if settings_service is not None:
            self._lock_settings_controls()
            self._apply_settings()
            settings_service.add_listener(self._settings_changed.emit)
            self._settings_changed.connect(self._on_settings_changed, Qt.ConnectionType.QueuedConnection)

        # Operators mark loaded/empty slots by tapping the slot cards.
        jig_grid.slot_clicked.connect(self._on_slot_clicked)

        # The advance worker is only gone once its thread is cleaned up.
        operations.operation_removed.connect(lambda _operation_id: self._schedule_render())

        self._readiness_timer = QTimer(self)
        self._readiness_timer.setSingleShot(True)
        self._readiness_timer.setInterval(400)
        self._readiness_timer.timeout.connect(lambda: self.check_station(top_up=False))

        self.render()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self, stock_fw: Path | None, pricol_fw: Path | None) -> None:
        layout = QVBoxLayout(self)

        if self._simulation:
            banner = QLabel("SIMULATION MODE - no hardware is used; records are not production evidence")
            banner.setStyleSheet(f"background: {AMBER}; color: white; font-weight: 700; padding: 6px; border-radius: 4px;")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(banner)

        self.step_rail = StepRail(self)
        layout.addWidget(self.step_rail)

        self.instruction_frame = QFrame(self)
        frame_layout = QVBoxLayout(self.instruction_frame)
        self.headline_label = QLabel("")
        self.headline_label.setObjectName("headline")
        self.instruction_label = QLabel("")
        self.instruction_label.setWordWrap(True)
        frame_layout.addWidget(self.headline_label)
        frame_layout.addWidget(self.instruction_label)
        layout.addWidget(self.instruction_frame)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {RED}; font-weight: 600;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.primary_button = QPushButton("START")
        self.primary_button.setMinimumHeight(52)
        self.primary_button.setMinimumWidth(260)
        self.primary_button.setStyleSheet(
            "QPushButton { font-size: 16px; font-weight: 700; color: white;"
            f" background: {BLUE}; border-radius: 6px; padding: 8px 18px; }}"
            "QPushButton:disabled { background: #cbd5e1; color: #475569; }"
        )
        self.primary_button.clicked.connect(self._on_primary)
        actions.addWidget(self.primary_button)

        # USB keyboard-wedge QR scanner: hex-only, last N characters,
        # completes by itself when the scan burst ends.
        self.qr_input = ScannerLineEdit(self._controller.qr_format or MODULE_QR_FORMAT, debounce_ms=self._scan_debounce_ms)
        self.qr_input.setPlaceholderText("Scanner disabled")
        self.qr_input.setMinimumHeight(44)
        self.qr_input.setStyleSheet("font-size: 16px;")
        self.qr_input.scanned.connect(self._on_scan_code)
        self.qr_input.rejected.connect(self._on_scan_rejected)
        self.qr_input.repeated.connect(lambda code: logger.info("Repeated read of QR %s discarded", code))
        actions.addWidget(self.qr_input, 1)

        self.abort_button = QPushButton("Supervisor: abort batch")
        self.abort_button.clicked.connect(self._on_abort)
        self.abort_button.hide()
        actions.addWidget(self.abort_button)
        layout.addLayout(actions)

        self.setup_box = self._build_setup(stock_fw, pricol_fw)
        layout.addWidget(self.setup_box)

        layout.addWidget(self._jig_grid)
        layout.addStretch(1)

    def _build_setup(self, stock_fw: Path | None, pricol_fw: Path | None) -> QGroupBox:
        box = QGroupBox("Batch setup")
        outer = QHBoxLayout(box)

        form = QFormLayout()
        self.operator_input = QLineEdit()
        self.operator_input.setPlaceholderText("Scan badge or enter operator ID")
        form.addRow("Operator", self.operator_input)

        self.batch_label_input = QLineEdit()
        self.batch_label_input.setPlaceholderText("Batch / lot identification")
        form.addRow("Batch", self.batch_label_input)

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.stock_only_radio = QRadioButton("Stock only")
        self.full_radio = QRadioButton("Stock + Pricol")
        self.full_radio.setChecked(True)
        for button in (self.stock_only_radio, self.full_radio):
            self.mode_group.addButton(button)
            mode_row.addWidget(button)
            button.toggled.connect(self._on_setup_changed)
        mode_row.addStretch()
        form.addRow("Programming", mode_row)

        self.stock_fw_input, stock_row = self._file_row(stock_fw)
        form.addRow("Stock firmware", stock_row)
        self.pricol_fw_input, pricol_row = self._file_row(pricol_fw)
        form.addRow("Pricol firmware", pricol_row)

        slot_row = QHBoxLayout()
        self.slot_checks: dict[int, QCheckBox] = {}
        for number in range(1, self._controller.slot_count + 1):
            check = QCheckBox(str(number))
            check.setEnabled(False)
            check.toggled.connect(self._on_setup_changed)
            self.slot_checks[number] = check
            slot_row.addWidget(check)
        slot_row.addStretch()
        form.addRow("Loaded slots", slot_row)
        outer.addLayout(form, 3)

        right = QVBoxLayout()
        self.readiness_summary = QLabel("Station not checked")
        self.readiness_summary.setStyleSheet("font-weight: 700;")
        right.addWidget(self.readiness_summary)

        self.readiness_table = QTableWidget(0, 3)
        self.readiness_table.setHorizontalHeaderLabels(["Check", "Status", "Detail"])
        self.readiness_table.verticalHeader().setVisible(False)
        self.readiness_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.readiness_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        right.addWidget(self.readiness_table)

        self.check_button = QPushButton("Check station / refill MAC cache")
        self.check_button.clicked.connect(lambda: self.check_station(top_up=True))
        right.addWidget(self.check_button)
        outer.addLayout(right, 4)
        return box

    def _file_row(self, default: Path | None) -> tuple[QLineEdit, QWidget]:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(str(default) if default else "")
        browse = QPushButton("Browse...")
        self._browse_buttons.append(browse)

        def choose() -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Select firmware image", edit.text(), "Firmware (*.bin);;All files (*)")
            if path:
                edit.setText(path)

        browse.clicked.connect(choose)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        return edit, container

    # ------------------------------------------------------------------
    # Setup / readiness
    # ------------------------------------------------------------------

    @property
    def selected_mode(self) -> ProgrammingMode:
        return ProgrammingMode.STOCK_ONLY if self.stock_only_radio.isChecked() else ProgrammingMode.STOCK_AND_PRICOL

    @property
    def selected_slots(self) -> tuple[int, ...]:
        return tuple(number for number, check in self.slot_checks.items() if check.isChecked() and check.isEnabled())

    def _on_setup_changed(self) -> None:
        self.pricol_fw_input.setEnabled(self.selected_mode is ProgrammingMode.STOCK_AND_PRICOL)
        if self._snapshot is None or not self._snapshot.has_batch:
            self._readiness_timer.start()
        self.render()

    def check_station(self, *, top_up: bool = False) -> None:
        """Evaluate READY on a worker; contacts the LAN server if top_up."""
        if self._shut_down:
            return
        if self._readiness_pending:
            self._readiness_rerun = True
            return
        mode = self.selected_mode
        occupied = self.selected_slots or None
        readiness = self._readiness

        def operation(_operation_id, _token, _progress):
            return readiness.evaluate(mode, occupied, top_up_macs=top_up)

        def configure(worker) -> None:
            # Bound methods: Qt drops these connections if the panel is
            # destroyed before the worker finishes.
            worker.succeeded.connect(self._on_readiness_result)
            worker.failed.connect(self._on_readiness_failure)
            worker.finished.connect(self._on_readiness_finished)

        try:
            self._operations.start(operation, operation_id=READINESS_OPERATION, configure=configure)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not start readiness check")
            self.readiness_summary.setText(f"Station check could not start: {exc}")
            return

        self._readiness_pending = True
        self.check_button.setEnabled(False)
        self.readiness_summary.setText("Checking station...")
        self.readiness_summary.setStyleSheet(f"color: {GREY}; font-weight: 700;")

    def _on_readiness(self, report: ReadinessReport) -> None:
        first = self._report is None
        self._report = report

        for number, check in self.slot_checks.items():
            enabled = self._slot_enabled(number)
            available = enabled and number in report.ports
            check.blockSignals(True)
            check.setEnabled(available)
            if first:
                check.setChecked(available)
            elif not available:
                check.setChecked(False)
            if not enabled:
                check.setToolTip("Disabled by the Supervisor in Production settings")
            else:
                check.setToolTip("Ready" if available else self._slot_reason(report, number))
            check.blockSignals(False)

        self.readiness_table.setRowCount(len(report.checks))
        for row, health in enumerate(report.checks):
            status = QTableWidgetItem(health.severity.value)
            status.setForeground(Qt.GlobalColor.white)
            status.setBackground(QColor(_SEVERITY[health.severity]))
            for column, item in enumerate((QTableWidgetItem(health.name), status, QTableWidgetItem(health.detail))):
                item.setToolTip(health.detail)
                self.readiness_table.setItem(row, column, item)

        if report.ready:
            self.readiness_summary.setText("READY - modules may be loaded")
            self.readiness_summary.setStyleSheet(f"color: {GREEN}; font-weight: 700;")
        else:
            self.readiness_summary.setText("NOT READY - " + "; ".join(report.problems))
            self.readiness_summary.setStyleSheet(f"color: {RED}; font-weight: 700;")
        self.readiness_summary.setWordWrap(True)

        if first and self.selected_slots:
            # Slots were just enabled; re-evaluate for the actual load.
            self._readiness_timer.start()
        self.render()

    @staticmethod
    def _slot_reason(report: ReadinessReport, number: int) -> str:
        if report.eligibility is None:
            return "Unavailable"
        try:
            return report.eligibility.get(number).reason
        except ValueError:
            return "Unavailable"

    def _on_readiness_result(self, result) -> None:
        if not self._shut_down:
            self._on_readiness(result.value)

    def _on_readiness_failure(self, failure) -> None:
        if not self._shut_down:
            self._on_readiness_failed(failure.user_message)

    def shutdown(self) -> None:
        """Stop background checks before the panel goes away."""
        self._shut_down = True
        self._readiness_timer.stop()

    def _on_readiness_failed(self, message: str) -> None:
        self._report = None
        self.readiness_summary.setText(f"Station check failed: {message}")
        self.readiness_summary.setStyleSheet(f"color: {RED}; font-weight: 700;")

    def _on_readiness_finished(self, _operation_id: str) -> None:
        self._readiness_pending = False
        self.check_button.setEnabled(True)
        if self._readiness_rerun:
            self._readiness_rerun = False
            self._readiness_timer.start()
        self.render()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_primary(self) -> None:
        snapshot = self._controller.snapshot()

        if not snapshot.has_batch:
            self._start_batch()
            return

        workflow = snapshot.workflow
        assert workflow is not None

        if workflow.completed or workflow.batch_state is BatchState.ABORTED:
            self._release_batch()
            return

        self._run_advance()

    def _start_batch(self) -> None:
        if self._report is None:
            self._action_error("Check the station before starting")
            return

        stock = self.stock_fw_input.text().strip()
        pricol = self.pricol_fw_input.text().strip()
        try:
            self._controller.start_batch(
                occupied=self.selected_slots,
                ports=self._report.ports,
                mode=self.selected_mode,
                batch_label=self.batch_label_input.text(),
                operator_id=self.operator_input.text(),
                stock_firmware=Path(stock) if stock else None,
                pricol_firmware=Path(pricol) if pricol else None,
            )
        except ProductionError as exc:
            self._action_error(str(exc))
            return
        self.render()

    def _release_batch(self) -> None:
        try:
            self._controller.release_batch()
        except ProductionError as exc:
            self._action_error(str(exc))
            return
        self.batch_label_input.clear()
        self.render()
        self.check_station(top_up=True)

    def _run_advance(self) -> None:
        controller = self._controller

        def operation(_operation_id, token, _progress):
            controller.advance(token)

        try:
            self._operations.start(operation, operation_id=ADVANCE_OPERATION)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not start production step")
            self._action_error(f"Could not start: {exc}")
            return
        self.primary_button.setEnabled(False)

    def _on_scan_rejected(self, raw: str) -> None:
        self._action_error(
            f"Invalid QR data {raw.strip()!r}: expected {self.qr_input.scan_format.description}. Scan again."
        )
        QApplication.beep()

    def _on_scan_code(self, text: str) -> None:
        if not text.strip():
            return
        try:
            slot = self._controller.scan_qr(text)
        except ProductionError as exc:
            # A refused scan may be retried at once with the same label.
            self.qr_input.forget_last_scan()
            self._action_error(str(exc))
            QApplication.beep()
            return
        logger.info("QR bound | slot=%s", slot)
        self._local_error = None
        self.render()

    def _on_access_changed(self) -> None:
        if self._snapshot is None or not self._snapshot.has_batch:
            self._readiness_timer.start()
        self.render()

    def _on_abort(self) -> None:
        authorizer = None
        if self._access is not None:
            dialog = SupervisorAuthorizationDialog(self._access, "abort this batch", self)
            if not dialog.exec() or dialog.result_user is None:
                return
            authorizer = dialog.result_user
            supervisor = authorizer.user_id
        else:
            supervisor, ok = QInputDialog.getText(self, "Supervisor authorization", "Supervisor ID:")
            if not ok or not supervisor.strip():
                return
        reason, ok = QInputDialog.getText(self, "Abort batch", "Reason for aborting this batch:")
        if not ok or not reason.strip():
            return
        answer = QMessageBox.question(
            self,
            "Abort batch",
            "Abort this batch? Every slot keeps its recorded outcome and "
            "no MAC address is returned to the pool.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        try:
            self._controller.abort_batch(reason=reason, actor_id=supervisor.strip(), supervisor=authorizer)
        except ProductionError as exc:
            self._action_error(str(exc))
            return
        self.render()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _schedule_render(self) -> None:
        if self._render_pending:
            return
        self._render_pending = True
        QTimer.singleShot(30, self.render)

    def render(self) -> None:
        self._render_pending = False
        snapshot = self._controller.snapshot()
        self._snapshot = snapshot
        running = snapshot.running or self._operations.get(ADVANCE_OPERATION) is not None

        if not snapshot.has_batch:
            self._render_setup(snapshot)
            return

        workflow = snapshot.workflow
        assert workflow is not None
        stock_only = snapshot.programming_mode is ProgrammingMode.STOCK_ONLY
        blocked = workflow.requires_supervisor or snapshot.last_error is not None
        self.step_rail.set_step(
            _STEP_FOR_STATE.get(workflow.batch_state, self.step_rail.active_step),
            stock_only=stock_only,
            blocked=blocked,
        )

        self.setup_box.setEnabled(False)
        self.setup_box.hide()

        headline = workflow.headline
        instruction = workflow.instruction
        if workflow.batch_state is BatchState.ABORTED:
            headline = "BATCH ABORTED"
            instruction = "Remove the modules, then prepare the next batch. " + (workflow.instruction or "")
        self._set_banner(
            headline,
            instruction,
            RED if workflow.requires_supervisor else GREEN if workflow.completed else GREY if workflow.automatic_operation else BLUE,
        )

        retry = workflow.automatic_operation and snapshot.last_error is not None
        if running:
            label, enabled = "WORKING...", False
        elif retry:
            label, enabled = "RETRY", True
        elif workflow.completed or workflow.batch_state is BatchState.ABORTED:
            label, enabled = "PREPARE NEXT BATCH", True
        else:
            label, enabled = workflow.button_label, workflow.allow_start_button
        self.primary_button.setText(label)
        self.primary_button.setEnabled(enabled)

        self.abort_button.setVisible(workflow.requires_supervisor and workflow.batch_state is not BatchState.ABORTED and not running)

        scanning = workflow.scanner_enabled and not running
        self.qr_input.setEnabled(scanning)
        self.qr_input.setPlaceholderText(
            f"Scan QR of module in SLOT {workflow.expected_slot}" if scanning else "Scanner disabled"
        )
        if scanning:
            self.qr_input.setFocus()

        self._show_error(self._current_error(snapshot))

        if snapshot.jig is not None and snapshot.jig.station_id == self._station_id:
            self._jig_grid.update_view(snapshot.jig)

    def _render_setup(self, snapshot: ProductionSnapshot) -> None:
        if self._access is not None:
            operator = self._access.operator
            self.operator_input.setText(operator.label if operator else "")
        self.step_rail.set_step("LOAD & READY", stock_only=self.selected_mode is ProgrammingMode.STOCK_ONLY, blocked=False)
        self.setup_box.setVisible(self._supervisor_view)
        self.setup_box.setEnabled(True)
        self.abort_button.hide()
        self.qr_input.setEnabled(False)
        self.qr_input.setPlaceholderText("Scanner disabled")
        self._render_load_grid()

        ready = self._report is not None and self._report.ready and bool(self.selected_slots) and not self._readiness_pending
        operator = not self._supervisor_view
        if self._report is None:
            if operator:
                self._set_banner("CHECKING JIG...", "Please wait while the station checks the jig.", GREY)
            else:
                self._set_banner("CHECK STATION", "Press 'Check station' to validate the jig, Golden Rig and MAC cache.", BLUE)
        elif not self._report.ready:
            if operator:
                problem = self._report.problems[0] if self._report.problems else "station check failed"
                self._set_banner("JIG NOT READY", f"Call the Supervisor. ({problem})", RED)
            else:
                self._set_banner("STATION NOT READY", "Resolve the failed checks below, or call the Supervisor.", RED)
        elif not self.selected_slots:
            self._set_banner("JIG READY - LOAD MODULES", "Load modules into the jig, then tap each slot that holds a module.", BLUE)
        else:
            slots = ", ".join(map(str, self.selected_slots))
            self._set_banner("JIG READY - PRESS START", f"Modules loaded in slot(s) {slots}. Tap a slot to change, then press START.", GREEN)

        self.primary_button.setText("START")
        self.primary_button.setEnabled(ready)
        self._show_error(self._current_error(snapshot))

    # ------------------------------------------------------------------
    # Roles, settings and slot loading
    # ------------------------------------------------------------------

    def set_supervisor_view(self, supervisor: bool) -> None:
        self._supervisor_view = supervisor
        self.render()

    @property
    def supervisor_view(self) -> bool:
        return self._supervisor_view

    def _slot_enabled(self, number: int) -> bool:
        return self._settings_service is None or number in self._settings_service.current().enabled_slots

    def _lock_settings_controls(self) -> None:
        self.setup_box.setTitle("Batch setup (from Production settings - Supervisor only)")
        for button in (self.stock_only_radio, self.full_radio):
            button.setEnabled(False)
        for edit in (self.stock_fw_input, self.pricol_fw_input, self.batch_label_input):
            edit.setReadOnly(True)
        for button in self._browse_buttons:
            button.hide()

    def _apply_settings(self) -> None:
        settings = self._settings_service.current()
        self.stock_only_radio.blockSignals(True)
        self.full_radio.blockSignals(True)
        self.stock_only_radio.setChecked(settings.mode is ProgrammingMode.STOCK_ONLY)
        self.full_radio.setChecked(settings.mode is ProgrammingMode.STOCK_AND_PRICOL)
        self.stock_only_radio.blockSignals(False)
        self.full_radio.blockSignals(False)
        self.stock_fw_input.setText(str(settings.stock_firmware or ""))
        self.pricol_fw_input.setText(str(settings.pricol_firmware or ""))
        self.batch_label_input.setText(settings.lot or "")

    def _on_settings_changed(self) -> None:
        self._apply_settings()
        if self._snapshot is None or not self._snapshot.has_batch:
            self._readiness_timer.start()
        self.render()

    def _on_slot_clicked(self, number: int) -> None:
        snapshot = self._controller.snapshot()
        if snapshot.has_batch or snapshot.running:
            return
        check = self.slot_checks.get(number)
        if check is not None and check.isEnabled():
            check.toggle()

    def _render_load_grid(self) -> None:
        report = self._report
        views = []
        for number, check in self.slot_checks.items():
            if report is None:
                views.append(SlotView(number, SlotVisualStatus.EMPTY, "Checking...", detail=""))
            elif not check.isEnabled():
                reason = check.toolTip() or "Not available"
                views.append(SlotView(number, SlotVisualStatus.UNAVAILABLE, "Not available", detail=reason))
            elif check.isChecked():
                views.append(SlotView(number, SlotVisualStatus.READY, "Module loaded", detail="Tap to mark empty"))
            else:
                views.append(SlotView(number, SlotVisualStatus.EMPTY, "Empty", detail="Tap to mark loaded"))
        available = sum(1 for check in self.slot_checks.values() if check.isEnabled())
        loaded = len(self.selected_slots)
        self._jig_grid.show_slots(views, f"Loaded: {loaded} of {available} available slots - tap a slot to change")

    def _set_banner(self, headline: str, instruction: str, colour: str) -> None:
        self.headline_label.setText(headline)
        self.instruction_label.setText(instruction)
        self.instruction_frame.setStyleSheet(
            f"QFrame {{ border: 2px solid {colour}; border-radius: 8px; }}"
            "QLabel { border: none; }"
        )
        self.headline_label.setStyleSheet(f"font-size: 22px; font-weight: 800; color: {colour};")
        self.instruction_label.setStyleSheet("font-size: 15px;")

    def _action_error(self, message: str) -> None:
        snapshot = self._controller.snapshot()
        self._local_error = message
        self._error_state = snapshot.workflow.batch_state if snapshot.workflow else None
        self._show_error(message)

    def _current_error(self, snapshot: ProductionSnapshot) -> str | None:
        state = snapshot.workflow.batch_state if snapshot.workflow else None
        if self._local_error is not None and state != self._error_state:
            self._local_error = None
        return snapshot.last_error or self._local_error

    def _show_error(self, message: str | None) -> None:
        if message:
            self.error_label.setText(message)
            self.error_label.show()
        else:
            self.error_label.clear()
            self.error_label.hide()

