""" The station application's main window

Two tabs:
    Production      -- the guided operator workflow (ProductionPanel)
    Station status  -- environment health and jig serial topology

No business logic lives here -- the window asks services for state and
renders it. Workflow rules live in ProductionController and the domain.
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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.bootstrap import AppContext
from stationapp.services.health import HealthCheck, Severity, run_all_checks, summarise

import logging
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox


from stationapp.concurrency.manager import OperationManager
from stationapp.services.serial_topology import (StationSerialTopologyService, SlotTopologyStatus, SerialTopology)

from stationapp.services.slot_eligibility import (JigEligibility, SlotEligibilityStatus)
from stationapp.domain.batch import Batch

from stationapp.services.jig_status_projection import (JigStatusProjection, JigView)
from stationapp.ui.widgets.jig_slot_grid import (JigSlotGrid)
from stationapp.ui.widgets.production_panel import ProductionPanel

logger = logging.getLogger(__name__)



_SEVERITY_COLOURS = {
    Severity.OK: QColor("#1b7f3b"),     # Green
    Severity.WARN: QColor("#b26a00"),    # Amber
    Severity.FAIL: QColor("#b3261e")    # red
}

_TOPOLOGY_COLOURS = {
    SlotTopologyStatus.READY: QColor("#1b7f3b"),     # Green
    SlotTopologyStatus.UNBOUND: QColor("#7a7a7a"),    # Grey
    SlotTopologyStatus.MISSING: QColor("#b3261e"),    # red
    SlotTopologyStatus.MOVED: QColor("#b26a00")       # Amber
}

_ELIGIBILITY_COLOURS = {
    SlotEligibilityStatus.ELIGIBLE: QColor("#1b7f3b"),
    SlotEligibilityStatus.BLOCKED_UNBOUND: QColor("#7a7a7a"),
    SlotEligibilityStatus.BLOCKED_MISSING: QColor("#b3261e"),
    SlotEligibilityStatus.BLOCKED_MOVED: QColor("#b26a00"),
}

# Health is re-checked on this interval while window is open
_HEALTH_REFRESH_MS = 30_000
_SERIAL_TOPOLOGY_REFRESH_MS = 5_000

class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._operation_manager = OperationManager(self)
        self._production_panel: ProductionPanel | None = None
        self._closing = False
        self._context = context
        self._last_topology: SerialTopology | None = None
        self._last_eligibility: (JigEligibility | None) = None

        self.setWindowTitle(
        f"Station App {context.app_version} - { context.station_label}"
        )
        self.resize(1150, 850)
        self._jig_status_projection = JigStatusProjection()
        self._active_batch_id: str | None = None
        self._build_ui()
        self.refresh_health()
        self.refresh_serial_topology()
        if self._production_panel is not None:
            # Initial READY evaluation, topping up the MAC cache if needed.
            self._production_panel.check_station(top_up=True)
        #Periodic re-check: Mpcli can be closed, a USB cable pulled. The
        # station's condition is not a startup time fact
        self._timer = QTimer(self)
        self._timer.setInterval(_HEALTH_REFRESH_MS)
        self._timer.timeout.connect(self.refresh_health)
        self._timer.start()
        self._topology_timer = QTimer(self)
        self._topology_timer.setInterval(_SERIAL_TOPOLOGY_REFRESH_MS)
        self._topology_timer.start()

        #-----------------UI CONSTRUCTION -------------------------------
    def _build_ui(self) -> None:
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        self._tabs = tabs

        self._jig_slot_grid = JigSlotGrid(self._context.settings.jig_positions, self)
        tabs.addTab(self._build_production_tab(), "Production")

        central = QWidget()
        tabs.addTab(central, "Station status")
        layout = QVBoxLayout(central)

        title = QLabel(f"Station {self._context.station_label}")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
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

        slot_title = QLabel("Jig Serial Topology")
        slot_title_font = QFont()
        slot_title_font.setBold(True)
        slot_title.setFont(slot_title_font)
        layout.addWidget(slot_title)

        self._slot_summary_label = QLabel("Checking Jig...")
        layout.addWidget(self._slot_summary_label)
        self._slot_table = QTableWidget(0, 6)
        self._slot_table.setHorizontalHeaderLabels(
            [
                "Slot",
                "Topology",
                "Eligibility",
                "COM Port",
                "USB Location",
                "Identity",
            ]
        )
        self._slot_table.verticalHeader().setVisible(False)
        self._slot_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        slot_header = (self._slot_table.horizontalHeader())
        slot_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        slot_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        slot_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        slot_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        slot_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._slot_table)

        header = self._checks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._checks_table)

        footer = QHBoxLayout()
        self._stage_label = QLabel(
        (
            "Stage 1 - Local operation"
            if not self._context.settings.is_stage_two
            else (
                    f"LAN server: {self._context.settings.lan_server_url}"
                )
            )
        )
        footer.addWidget(self._stage_label)
        footer.addStretch()

        self._refresh_button = QPushButton("Re-check now")
        self._refresh_button.clicked.connect(self.refresh_health)
        footer.addWidget(self._refresh_button)

        self._refresh_topology_button = QPushButton("Re-check Jig")
        self._refresh_topology_button.clicked.connect(self.refresh_serial_topology)
        footer.addWidget(self._refresh_topology_button)

        layout.addLayout(footer)

    def _build_production_tab(self) -> QWidget:
        context = self._context
        if context.production is None or context.readiness is None:
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.addWidget(QLabel("Production services are not available in this session."))
            layout.addWidget(self._jig_slot_grid, 1)
            return container

        self._production_panel = ProductionPanel(
            controller=context.production,
            readiness=context.readiness,
            operations=self._operation_manager,
            jig_grid=self._jig_slot_grid,
            station_id=context.settings.station_id,
            default_stock_firmware=context.settings.stock_firmware_path,
            default_pricol_firmware=context.settings.pricol_firmware_path,
            simulation=context.simulation,
            parent=self,
        )
        if context.recovered_macs:
            QMessageBox.warning(
                self,
                "Startup recovery",
                f"{context.recovered_macs} MAC address(es) were left in PROGRAMMING when "
                "the application last stopped. They have been placed on HOLD and will not "
                "be reused. Inform the Supervisor.",
            )
        return self._production_panel

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
                
                
    def refresh_serial_topology(self) -> None:
        """Refresh and render current jig serial topology."""

        self._refresh_topology_button.setEnabled(False)

        try:
            topology = (self._context.serial_topology_service.refresh())
            eligibility = (self._context.slot_eligibility_service.evaluate(topology))
            self._last_topology = topology
            self._last_eligibility = eligibility
            self._render_serial_topology(topology)
            if self._last_eligibility is not None:
                eligible = (self._last_eligibility.eligible_count)
                total = len(self._last_eligibility.slots)
                blocked = (self._last_eligibility.blocked_count)
                if blocked == 0:
                    self._slot_summary_label.setText(f"All {total} slots available")
                    self._slot_summary_label.setStyleSheet("color: #1b7f3b;")
                elif eligible > 0:
                    self._slot_summary_label.setText(f"{eligible}/{total} slots available; {blocked} blocked")
                    self._slot_summary_label.setStyleSheet("color: #b26a00;")
                else:
                    self._slot_summary_label.setText(f"No slots available; {blocked}/{total} blocked")
                    self._slot_summary_label.setStyleSheet("color: #b3261e;")

        except Exception as exc:
            logger.exception("Serial topology refresh failed")

            self._slot_summary_label.setText(f"Topology check failed: {exc}")
            self._slot_summary_label.setStyleSheet("color: #b3261e;")

        finally:
            self._refresh_topology_button.setEnabled(True)


    def _render_serial_topology(self,topology: SerialTopology) -> None:

        self._last_topology = topology
        self._slot_table.setRowCount(len(topology.slots))

        for row, slot in enumerate(topology.slots):
            eligibility = None

            if self._last_eligibility is not None:
                eligibility = (self._last_eligibility.get(slot.slot_number))

            slot_item = QTableWidgetItem(str(slot.slot_number))
            status_item = QTableWidgetItem(slot.status.value)

            status_item.setForeground(_TOPOLOGY_COLOURS[slot.status])
            eligibility_item = QTableWidgetItem((eligibility.status.value if eligibility is not None else "-"))

            if eligibility is not None:
                eligibility_item.setForeground(_ELIGIBILITY_COLOURS[eligibility.status])

                eligibility_item.setToolTip(eligibility.reason)

            # Important:
            # only show the COM port when the slot is actually eligible.
            if (eligibility is not None and eligibility.eligible):
                visible_com = (eligibility.com_port or "-")
            else:
                visible_com = "-"

            com_item = QTableWidgetItem(visible_com)
            location = "-"

            if slot.port is not None:
                location = (slot.port.identity.location or "-")

            location_item = QTableWidgetItem(location)

            identity_item = QTableWidgetItem(slot.stable_key or "-")

            items = (
                slot_item,
                status_item,
                eligibility_item,
                com_item,
                location_item,
                identity_item,
            )

            # Show topology diagnostic on every cell.
            for item in items:
                item.setToolTip(slot.message)

            # Keep the more specific eligibility message
            # on the eligibility cell.
            if eligibility is not None:
                eligibility_item.setToolTip(eligibility.reason)

            # Optional visual indication for blocked slots.
            if (eligibility is not None and not eligibility.eligible):
                for item in items:
                    font = item.font()
                    font.setItalic(True)
                    item.setFont(font)

            for column, item in enumerate(items):
                self._slot_table.setItem(row, column, item)

        # ----- Summary ----------------------------------------

        if self._last_eligibility is None:
            self._slot_summary_label.setText("Jig eligibility unavailable")
            self._slot_summary_label.setStyleSheet("color: #b3261e;")
            return
        eligible = (self._last_eligibility.eligible_count)
        total = len(self._last_eligibility.slots)

        blocked = (self._last_eligibility.blocked_count)

        if blocked == 0:
            self._slot_summary_label.setText(f"All {total} slots available")
            self._slot_summary_label.setStyleSheet("color: #1b7f3b;")
        elif eligible > 0:
            self._slot_summary_label.setText(f"{eligible}/{total} slots available; {blocked} blocked")
            self._slot_summary_label.setStyleSheet("color: #b26a00;")
        else:
            self._slot_summary_label.setText(f"No slots available; {blocked}/{total} blocked")
            self._slot_summary_label.setStyleSheet("color: #b3261e;")
        
    def closeEvent(self, event: QCloseEvent) -> None:
        """ Request cooperative shutdown of all active oeprations """
        
        if self._operation_manager.active_count == 0:
            event.accept()
            return 
            
        answer = QMessageBox.question(self, "Operations are still running",
                (
                    "A station operation is still active.\n\n"
                    "Closing now cancels it. Any module being programmed will be "
                    "placed on HOLD and its MAC address quarantined.\n\n"
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
        self._topology_timer.stop()
        self._operation_manager.cancel_all()
        
        # MPCLI observes cancellation through its process runner and is
        # terminated within its grace period.

        
        if not self._operation_manager.wait_for_all(timeout_ms=15_000):
            self._closing = False
            self._timer.start()
            self._topology_timer.start()
            QMessageBox.critical(self, "Unable to close safely", 
                                 (
                                    "An operation did not stop safely within 15 seconds. \n"
                                    "The application will remain open."
                                 ),
                            )
            event.ignore()
            return 
            
        event.accept()

    
    def show_batch_snapshot(self, view: JigView) -> None:
        """Render an already-projected manufacturing snapshot.

        Call this only on the Qt GUI thread.
        """
        if view.station_id != self._context.settings.station_id:
            raise ValueError("Batch belongs to another station")

        if view.jig_id != self._context.settings.jig_id:
            raise ValueError("Batch belongs to another jig")

        self._jig_slot_grid.update_view(view)
        self._active_batch_id = view.batch_id


    def show_batch(self, batch: Batch) -> None:
        """Convenience adapter for GUI-thread batch updates."""
        self.show_batch_snapshot(self._jig_status_projection.project(batch))


    def clear_active_batch(self) -> None:
        """Clear display after the production controller closes a batch."""
        self._active_batch_id = None
        self._jig_slot_grid.clear_batch()
    
       
            
            
