"""Supervisor page: browse the local production records.

Search by date, MAC, Operator, module QR and Batch Code (any
combination), see every module that matches, open a module's full event
history, and export the result to CSV. Read-only.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.services.history import DeviceHistoryService
from stationapp.services.production_records import (
    COLUMNS,
    ProductionRecord,
    ProductionRecordService,
    RecordFilter,
    export_csv,
    format_value,
)

logger = logging.getLogger(__name__)

GREEN = QColor("#166534")
RED = QColor("#991b1b")
AMBER = QColor("#92400e")
GREY = "#64748b"

RANGE_TODAY = "Today"
RANGE_YESTERDAY = "Yesterday"
RANGE_7 = "Last 7 days"
RANGE_30 = "Last 30 days"
RANGE_CUSTOM = "Custom dates"
RANGE_ALL = "All dates"

_PASS_WORDS = {"PASS", "PASSED", "COMMITTED", "FUNCTIONAL_TEST_PASSED", "STOCK_RF_CONFIRMED"}
_FAIL_WORDS = {"FAIL", "FAILED", "HOLD"}


class ProductionRecordsPage(QWidget):
    def __init__(self, records: ProductionRecordService, history: DeviceHistoryService | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._records = records
        self._history = history
        self._rows: tuple[ProductionRecord, ...] = ()
        self._searched_once = False

        layout = QVBoxLayout(self)

        filters = QGroupBox("Search production records")
        grid = QGridLayout(filters)

        self.range_input = QComboBox()
        self.range_input.addItems([RANGE_TODAY, RANGE_YESTERDAY, RANGE_7, RANGE_30, RANGE_CUSTOM, RANGE_ALL])
        self.range_input.setCurrentText(RANGE_7)
        self.range_input.currentTextChanged.connect(self._range_changed)
        self.from_input = QDateEdit()
        self.to_input = QDateEdit()
        for edit in (self.from_input, self.to_input):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setDate(QDate.currentDate())
        grid.addWidget(QLabel("Date"), 0, 0)
        grid.addWidget(self.range_input, 0, 1)
        grid.addWidget(QLabel("From"), 0, 2)
        grid.addWidget(self.from_input, 0, 3)
        grid.addWidget(QLabel("To"), 0, 4)
        grid.addWidget(self.to_input, 0, 5)

        self.mac_input = QLineEdit()
        self.mac_input.setPlaceholderText("Full MAC or its last bytes, e.g. 12:34:56")
        self.operator_input = QComboBox()
        self.operator_input.setEditable(True)
        self.operator_input.lineEdit().setPlaceholderText("Operator ID or name")
        self.qr_input = QLineEdit()
        self.qr_input.setPlaceholderText("Module QR (full or part)")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Batch Code")
        grid.addWidget(QLabel("MAC ID"), 1, 0)
        grid.addWidget(self.mac_input, 1, 1)
        grid.addWidget(QLabel("Operator"), 1, 2)
        grid.addWidget(self.operator_input, 1, 3)
        grid.addWidget(QLabel("QR Code"), 2, 0)
        grid.addWidget(self.qr_input, 2, 1)
        grid.addWidget(QLabel("Batch Code"), 2, 2)
        grid.addWidget(self.code_input, 2, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 2)

        buttons = QHBoxLayout()
        self.search_button = QPushButton("Search")
        self.search_button.setDefault(True)
        self.search_button.clicked.connect(self.search)
        self.clear_button = QPushButton("Clear filters")
        self.clear_button.clicked.connect(self._clear)
        self.export_button = QPushButton("Export CSV...")
        self.export_button.clicked.connect(self._export)
        self.export_button.setEnabled(False)
        buttons.addWidget(self.search_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch()
        buttons.addWidget(self.export_button)
        grid.addLayout(buttons, 3, 0, 1, 6)
        layout.addWidget(filters)

        for edit in (self.mac_input, self.qr_input, self.code_input, self.operator_input.lineEdit()):
            edit.returnPressed.connect(self.search)

        self.summary = QLabel("Choose filters and press Search.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([title for _name, title in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_history)
        splitter.addWidget(self.table)

        history_box = QGroupBox("Module history (select a row)")
        history_layout = QVBoxLayout(history_box)
        self.history_table = QTableWidget(0, 3)
        self.history_table.setHorizontalHeaderLabels(["Time", "Event", "Detail"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        history_layout.addWidget(self.history_table)
        splitter.addWidget(history_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self._range_changed(self.range_input.currentText())

    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self._searched_once:
            self._load_operators()
            self.search()

    def _load_operators(self) -> None:
        current = self.operator_input.currentText()
        try:
            operators = self._records.operators()
        except Exception:  # noqa: BLE001 - the list is a convenience
            logger.exception("Could not list operators")
            return
        self.operator_input.clear()
        self.operator_input.addItem("")
        self.operator_input.addItems(operators)
        self.operator_input.setCurrentText(current)

    def _range_changed(self, text: str) -> None:
        custom = text == RANGE_CUSTOM
        self.from_input.setEnabled(custom)
        self.to_input.setEnabled(custom)
        start, end = self._dates()
        if not custom and start is not None and end is not None:
            self.from_input.setDate(QDate(start.year, start.month, start.day))
            self.to_input.setDate(QDate(end.year, end.month, end.day))

    def _dates(self) -> tuple[date | None, date | None]:
        today = date.today()
        choice = self.range_input.currentText()
        if choice == RANGE_TODAY:
            return today, today
        if choice == RANGE_YESTERDAY:
            day = today - timedelta(days=1)
            return day, day
        if choice == RANGE_7:
            return today - timedelta(days=6), today
        if choice == RANGE_30:
            return today - timedelta(days=29), today
        if choice == RANGE_CUSTOM:
            start = self.from_input.date().toPyDate()
            end = self.to_input.date().toPyDate()
            return (start, end) if start <= end else (end, start)
        return None, None

    def criteria(self) -> RecordFilter:
        start, end = self._dates()
        return RecordFilter(
            date_from=start,
            date_to=end,
            mac=self.mac_input.text(),
            operator=self.operator_input.currentText(),
            module_qr=self.qr_input.text(),
            batch_code=self.code_input.text(),
        )

    def search(self) -> None:
        self._searched_once = True
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self._records.search(self.criteria())
        except Exception as exc:  # noqa: BLE001 - show, do not crash the window
            logger.exception("Production record search failed")
            self.summary.setText(f"Search failed: {exc}")
            self.summary.setStyleSheet(f"color: {RED.name()}; font-weight: 600;")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self._rows = result.records
        self._render()
        self.summary.setStyleSheet("")
        self.summary.setText(self._summary_text(result.truncated))
        self.export_button.setEnabled(bool(self._rows))

    def _clear(self) -> None:
        self.mac_input.clear()
        self.qr_input.clear()
        self.code_input.clear()
        self.operator_input.setCurrentText("")
        self.range_input.setCurrentText(RANGE_7)
        self.search()

    # ------------------------------------------------------------------

    def _summary_text(self, truncated: bool) -> str:
        rows = self._rows
        if not rows:
            return "No records match these filters."
        trays = len({row.batch_id for row in rows})
        results = Counter(row.result for row in rows)
        parts = ", ".join(f"{name} {count}" for name, count in results.most_common())
        retried = sum(1 for row in rows if row.retries)
        text = f"{len(rows)} module(s) on {trays} tray(s): {parts}."
        if retried:
            text += f" {retried} module(s) were retried by the operator."
        if truncated:
            text += f" Only the newest {len(rows)} are shown; narrow the filters to see the rest."
        return text

    def _render(self) -> None:
        table = self.table
        table.setSortingEnabled(False)
        table.clearContents()
        table.setRowCount(len(self._rows))
        names = [name for name, _title in COLUMNS]
        for row_index, record in enumerate(self._rows):
            for column, name in enumerate(names):
                value = getattr(record, name)
                item = QTableWidgetItem(format_value(value))
                if name == "slot_number" or name == "retries":
                    item.setData(Qt.ItemDataRole.DisplayRole, int(value or 0))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row_index)
                colour = _colour(name, value)
                if colour is not None:
                    item.setForeground(colour)
                table.setItem(row_index, column, item)
        table.resizeColumnsToContents()
        table.setSortingEnabled(True)
        self.history_table.setRowCount(0)

    def _selected(self) -> ProductionRecord | None:
        items = self.table.selectedItems()
        if not items:
            return None
        first = self.table.item(items[0].row(), 0)
        index = first.data(Qt.ItemDataRole.UserRole) if first is not None else None
        if index is None or not 0 <= index < len(self._rows):
            return None
        return self._rows[index]

    def _show_history(self) -> None:
        record = self._selected()
        self.history_table.setRowCount(0)
        if record is None or self._history is None:
            return
        try:
            history = self._history.for_batch_slot(batch_id=record.batch_id, slot_number=record.slot_number)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Module history unavailable")
            self.history_table.setRowCount(1)
            self.history_table.setItem(0, 2, QTableWidgetItem(f"History unavailable: {exc}"))
            return
        events = history.events
        self.history_table.setRowCount(len(events))
        for row, entry in enumerate(events):
            when = entry.occurred_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            payload = {key: value for key, value in entry.payload.items() if value not in (None, "", [], {})}
            detail = json.dumps(payload, default=str, ensure_ascii=False)
            event_type = entry.event_type.value if hasattr(entry.event_type, "value") else str(entry.event_type)
            type_item = QTableWidgetItem(event_type)
            if event_type in {"STAGE_RETRY", "HOLD_PLACED"} or event_type.endswith("UNCERTAIN"):
                type_item.setForeground(AMBER)
            detail_item = QTableWidgetItem(detail)
            detail_item.setToolTip(detail)
            for column, item in enumerate((QTableWidgetItem(when), type_item, detail_item)):
                self.history_table.setItem(row, column, item)

    def _export(self) -> None:
        if not self._rows:
            return
        default = f"production_records_{date.today():%Y%m%d}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export production records", default, "CSV (*.csv)")
        if not path:
            return
        try:
            count = export_csv(self._rows, Path(path))
        except OSError as exc:
            self.summary.setText(f"Export failed: {exc}")
            self.summary.setStyleSheet(f"color: {RED.name()}; font-weight: 600;")
            return
        self.summary.setText(f"Exported {count} record(s) to {path}")
        self.summary.setStyleSheet(f"color: {GREEN.name()}; font-weight: 600;")


def _colour(name: str, value) -> QColor | None:
    if name in {"app_test", "dfu_test"} and isinstance(value, bool):
        return GREEN if value else RED
    if name in {"result", "rf_outcome"} and isinstance(value, str):
        if value.upper() in _PASS_WORDS:
            return GREEN
        if value.upper() in _FAIL_WORDS or "FAIL" in value.upper() or "HOLD" in value.upper():
            return RED
    if name == "retries" and value:
        return AMBER
    return None
