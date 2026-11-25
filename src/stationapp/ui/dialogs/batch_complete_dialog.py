"""Pop-up shown when a Batch Code (Batch/Lot) is complete.

PRINT BATCH writes the Batch/Lot's MAC ID list as an Excel workbook
(one A4 landscape page) and opens it, so the Operator can check or fill
in the header and print it from Excel. The dialog is not modal:
production carries on behind it.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from stationapp.services.batch_mac_list import BatchMacListService, open_document

logger = logging.getLogger(__name__)

GREEN = "#166534"
RED = "#991b1b"


class BatchCompleteDialog(QDialog):
    def __init__(self, service: BatchMacListService, batch_code: str, threshold: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._code = batch_code
        self.setWindowTitle("Batch/Lot complete")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        headline = QLabel(f"Batch/Lot <b>{batch_code}</b> is complete")
        headline.setTextFormat(Qt.TextFormat.RichText)
        headline.setStyleSheet(f"font-size: 20px; color: {GREEN};")
        layout.addWidget(headline)
        count = f"{threshold} passed modules. " if threshold else ""
        detail = QLabel(
            f"{count}Press PRINT BATCH to open the MAC ID list in Excel. "
            "Check the Batch/Lot No and Week No at the top, fill them in if they are empty, then print (Ctrl+P)."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet("font-size: 14px;")
        layout.addWidget(detail)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.print_button = QPushButton("PRINT BATCH")
        self.print_button.setMinimumHeight(48)
        self.print_button.setMinimumWidth(200)
        self.print_button.setStyleSheet(f"background: {GREEN}; color: white; font-size: 16px; font-weight: 700;")
        self.print_button.clicked.connect(self.print_batch)
        self.close_button = QPushButton("Close")
        self.close_button.setMinimumHeight(48)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.print_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def print_batch(self) -> None:
        try:
            path, mac_list = self._service.export(self._code)
        except (OSError, ValueError) as exc:
            logger.exception("Batch MAC list for %s failed", self._code)
            self._status(f"Could not make the MAC ID list: {exc}", RED)
            return
        try:
            open_document(path)
        except OSError as exc:
            logger.exception("Opening %s failed", path)
            self._status(f"The list was saved but could not be opened ({exc}). Open it from:\n{path}", RED)
            return
        self._status(f"{len(mac_list.macs)} MAC IDs opened in Excel. Saved as:\n{path}", GREEN)

    def _status(self, text: str, colour: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {colour}; font-weight: 600;")
        self.status_label.show()
