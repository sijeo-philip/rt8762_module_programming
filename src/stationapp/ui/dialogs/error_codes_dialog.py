"""Reference list of MPCLI error codes for operators and supervisors."""

from __future__ import annotations

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.drivers.mpcli.error_codes import MPCLI_ERROR_CODES


class ErrorCodesDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, *, highlight: int | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MPCLI error codes")
        self.resize(1000, 620)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "When programming fails, the slot shows e.g. <b>MPCLI ERR 6 (hci event error)</b>. "
            "Find the number below to see what it means and what to do. "
            "Codes are from the Realtek MPCLI User Guide, Table 5.1."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(MPCLI_ERROR_CODES), 4)
        self.table.setHorizontalHeaderLabels(["Code", "MPCLI error", "What it means", "What to do"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        for row, entry in enumerate(sorted(MPCLI_ERROR_CODES.values(), key=lambda item: item.code)):
            cells = (str(entry.code), entry.name, entry.meaning, entry.action)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if entry.code == highlight:
                    item.setBackground(QColor("#fef3c7"))
                self.table.setItem(row, column, item)
            if entry.code == highlight:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
        self.table.resizeRowsToContents()
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
