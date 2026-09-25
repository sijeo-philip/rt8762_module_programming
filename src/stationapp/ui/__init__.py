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

_SEVERITY_COLOURS = {
    Severity.OK: QColor("#1b7f3b"),     # Green
    Severity.WARN: QColor("#b26a00")    # Amber
    Severity.FAIL: QColor("#b3261e")    # red
}

# Health is re-checked on this interval while window is open
_HEALTH_REFRESH_MS = 30_000

class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
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
        self._summary_label.setFont(summar_font)
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
        self._stage_label = QLable(
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
        
