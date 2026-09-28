""" The station application's main window

Lesson 2 scope: Display environment health and prove Qt wiring works.
No business logic lives here -- the window asks a service for health
records and renders them. Every later lesson adds panels to this window,
never logic.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.bootstrap import AppContext
from stationapp.services.health import Healthcheck, Severity, run_all_checks, summarise

import logging
from PyQt6.QtWidgets import QProgressBar
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox


from stationapp.concurrency.events import( OperationFailure, OperationResult, ProgressEvent)
from stationapp.concurrency.manager import OperationHandle, OperationManager
from stationapp.services.demo_operation import make_demo_rf_test

logger = logging.getLogger(__name__)



_SEVERITY_COLOURS = {
    Severity.OK: QColor("#1b7f3b"),     # Green
    Severity.WARN: QColor("#b26a00"),    # Amber
    Severity.FAIL: QColor("#b3261e")    # red
}

# Health is re-checked on this interval while window is open
_HEALTH_REFRESH_MS = 30_000

class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._operation_manager = OperationManager(self)
        self._active_demo_id: str | None = None
        self._closing = False
        self._context = context

        self.setWindowTitle(
        f"Station App {context.app_version} - { context.station_label}"
        )
        self.resize(860, 520)
        self._build_ui()
        self.refresh_health()
        #Periodic re-check: Mpcli can be closed, a USB cable pulled. The
        # station's condition is not a startup time fact
        self._timer = QTimer(self)
        self._timer.setInterval(_HEALTH_REFRESH_MS)
        self._timer.timeout.connect(self.refresh_health)
        self._timer.start()

        #-----------------UI CONSTRUCTION -------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel(f"Station {self._context.station_label}")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        layout.addWidget(title)

        self._summary_label = QLabel("Checking...")
        summary_font = QFont()
        summary_font.setPointSize(11)
        summary_font.setBold(True)
        self._summary_label.setFont(summary_font)
        layout.addWidget(self._summary_label)

        self._checks_table = QTableWidget(0, 3)
        self._checks_table.setHorizontalHeaderLabels(["Check", "Status", "Detail"])
        self._checks_table.verticalHeader().setVisible(False)
        self._checks_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        header = self._checks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._checks_table)

        footer = QHBoxLayout()
        self._stage_label = QLabel(
        "Stage 1 - Local operation"
        if not self._context.settings.is_stage_two
        else f"LAN server: {self._context_settings.lan_server_url}"
        )
        footer.addWidget(self._stage_label)
        footer.addStretch()

        self._refresh_button = QPushButton("Re-check now")
        self._refresh_button.clicked.connect(self.refresh_health)
        footer.addWidget(self._refresh_button)

        layout.addLayout(footer)
        worker_title = QLabel("Concurrency Demonstration")
        worker_title_font = QFont()
        worker_title_font.setBold(True)
        worker_title.setFont(worker_title_font)
        layout.addWidget(worker_title)
        
        self._operation_label = QLabel("No Operation Running")
        layout.addWidget(self._operation_label)
        
        self._operation_progress = QProgressBar()
        self._operation_progress.setRange(0, 8)
        self._operation_progress.setValue(0)
        layout.addWidget(self._operation_progress)
        
        worker_buttons = QHBoxLayout()
        
        self._start_demo_button = QPushButton("Start Simulated RF  Test")
        self._start_demo_button.clicked.connect(self.start_demo_rf_test)
        worker_buttons.addWidget(self._start_demo_button)
        
        self._cancel_demo_button = QPushButton("Cancel")
        self._cancel_demo_button.setEnabled(False)
        self._cancel_demo_button.clicked.connect(self.cancel_demo_rf_test)
        worker_buttons.addWidget(self._cancel_demo_button)
        
        worker_buttons.addStretch()
        layout.addLayout(worker_buttons)

    def refresh_health(self) -> None:
        """ Run the health checks and render the result.
        Run synchronously for now. Once the LAN server arrives (Stage 2)
        thread -- but only then. Adding thread Machinery before there is
        anything slow to run would be premature.
        """
        checks = run_all_checks(self._context.settings)
        severity, summary = summarise(checks)

        self._summary_label.setText(summary)
        self._summary_label.setStyleSheet(
        f"color: {_SEVERITY_COLOURS[severity].name()};"
        )
        self._populate_table(checks)

    def _populate_table(self, checks: list[HealthCheck]) -> None:
        self._checks_table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            name_item = QTableWidgetItem(check.name)
            status_item = QTableWidgetItem(check.severity.value)
            status_item.setForeground(_SEVERITY_COLOURS[check.severity])
            detail_item = QTableWidgetItem(check.detail)

            for col, item in enumerate((name_item, status_item, detail_item)):
                self._checks_table.setItem(row, col, item)
                
                
    def start_demo_rf_test(self) -> None:
        if self._active_demo_id is not None:
            return
            
        mac_addresses = [
            f"AA:BB:CC:DD:EE:{suffix:02x}"
            for suffix in range(1, 9)
        ]
        
        operation_id = "demo-rf-test"
        
        def configure(worker) -> None:
            worker.started.connect(self._on_operation_started)
            worker.progress.connect(self._on_operation_progress)
            worker.succeeded.connect(self._on_operation_succeeded)
            worker.failed.connect(self._on_operation_failed)
            worker.cancelled.connect(self._on_operation_cancelled)
            worker.finished.connect(self._on_operation_finished)
            
        try:
            handle = self._operation_manager.start(make_demo_rf_test(mac_addresses), operation_id=operation_id, configure=configure)
        except Exception as exc:
            logger.exception("Could not start demonstration operation")
            self._operation_label.setText(f"Could not start: {exc}")
            return
            
        self._active_demo_id = handle.operation_id
        self._start_demo_button.setEnabled(False)
        self._cancel_demo_button.setEnabled(True)
        self._operation_progress.setValue(0)
        
        
    def cancel_demo_rf_test(self) -> None:
        if self._active_demo_id is None:
            return
            
        self._operation_label.setText("Cancellation Requested ...")
        self._cancel_demo_button.setEnabled(False)
        self._operation_manager.cancel(self._active_demo_id)
        
    def _on_operation_started(self, operation_id: str) -> None:
        if operation_id != self._active_demo_id:
            return 
        self._operation_label.setText("RF test started")
        
    def _on_operation_progress(self, event: ProgressEvent) -> None:
        if event.operation_id != self._active_demo_id:
            return 
        
        self._operation_label.setText(event.message)
        
        if event.completed is not None:
            self._operation_progress.setValue(event.completed)
            
    def _on_operation_succeeded(self, result: OperationResult) -> None:
        if result.operation_id != self._active_demo_id:
            return 
            
        passed = sum(1 for item in result.value if item.connected and item.disconnected)
        self._operation_label.setText(f"RF test completed: {passed}/{len(result.value)} passed."
                                      f"in {result.elapsed_seconds:.2f}s")
                                      
    def _on_operation_failed(self, failure: OperationFailure) -> None:
        if failure.operation_id != self._active_demo_id:
            return 
            
        self._operation_label.setText(f"Failed: {failure.user_message}")
        logger.error("Operation failure detail | operation_id=%s type=%s detail=%s",failure.operation_id, failure.error_type, failure.technical_message)
        
        
    def _on_operation_cancelled(self, operation_id:str) -> None:
        if operation_id != self._active_demo_id:
            return 
            
        self._operation_label.setText("RF Test Cancelled")
        
    def _on_operation_finished(self, operation_id: str) -> None:
        if operation_id != self._active_demo_id:
            return 
        self._active_demo_id= None
        
        if not self._closing:
            self._start_demo_button.setEnabled(True)
            self._cancel_demo_button.setEnabled(False)
            
            
    def closeEvent(self, event: QCloseEvent) -> None:
        """ Request cooperative shutdown of all active oeprations """
        
        if self._operation_manager.active_count == 0:
            event.accept()
            return 
            
            
        answer = QMessageBox.question(self, "Operations are still running",
                (
                    "A station operation is still active.\n\n"
                    "Cancel the operation and close the application?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
        )
        
        if answer is QMessageBox.StandardButton.No:
            event.ignore()
            return 
            
        self._closing = True
        self._timer.stop()
        self._operation_manager.cancel_all()
        
        # The demo operation observed cancellation every 50ms. Real drivers
        # receive their own shutdown budgets in Lesson 7-10
        
        if not self._operation_manager.wait_for_all(timeout_ms=5_000):
            self._closing = False
            self._timer.start()
            QMessageBox.critical(self, "Unable to close safely", 
                                 (
                                    "An operation did not stop safely within 5 seconds. \n"
                                    "The application will remain open."
                                 ),
                            )
            event.ignore()
            return 
            
        event.accept()
        
        
            
            
