"""Review the slot bindings proposed by a reference-module scan."""

from __future__ import annotations

from PyQt6.QtCore import Qt
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

from stationapp.services.slot_autobind import AutoBindPlan

GREEN, AMBER, RED = "#dcfce7", "#fef3c7", "#fee2e2"


class ReferenceBindingDialog(QDialog):
    """Slot | COM port | MAC read | Change, problems, and Apply / Cancel.

    current: slot -> what the slot is bound to now (USB identity or COM
    port), to show what changes.
    """

    def __init__(self, plan: AutoBindPlan, *, current: dict[int, str] | None = None, apply_label: str = "Apply bindings", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bind slots from reference modules")
        self.resize(820, 460)
        current = current or {}
        layout = QVBoxLayout(self)

        headline = QLabel(plan.summary())
        headline.setWordWrap(True)
        headline.setStyleSheet(f"font-weight:700;color:{'#166534' if plan.complete else '#92400e'};")
        layout.addWidget(headline)

        self.table = QTableWidget(plan.jig_positions, 5)
        self.table.setHorizontalHeaderLabels(["Slot", "COM port", "MAC read", "USB identity", "Change"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for row in range(plan.jig_positions):
            slot = row + 1
            reading = plan.assignments.get(slot)
            now = current.get(slot)
            if reading is None:
                cells = [str(slot), "-", "-", "-", f"not found - keeps {now}" if now else "not found - stays unbound"]
                colour = AMBER
            else:
                new = reading.stable_key or reading.device
                change = "unchanged" if now == new else (f"changes from {now}" if now else "new binding")
                cells = [str(slot), reading.device, reading.mac or "", reading.stable_key or "", change]
                colour = GREEN
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 4:
                    item.setBackground(QColor(colour))
                self.table.setItem(row, column, item)
        layout.addWidget(self.table, 1)

        if plan.problems:
            problems = QLabel("<b>Not used:</b><br>" + "<br>".join(_escape(text) for text in plan.problems))
            problems.setTextFormat(Qt.TextFormat.RichText)
            problems.setWordWrap(True)
            problems.setStyleSheet("color:#991b1b;")
            layout.addWidget(problems)

        note = QLabel("Only the slots found above are changed. Check them against the physical jig before applying.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#64748b;")
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton(apply_label, QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setEnabled(bool(plan.assignments))
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
