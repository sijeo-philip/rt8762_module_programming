
"""Qt graphical display of jig manufacturing status."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from stationapp.services.jig_status_projection import (
    JigView,
    SlotView,
    SlotVisualStatus,
)


COLOURS = {
    SlotVisualStatus.EMPTY: ("#64748b", "#e2e8f0"),
    SlotVisualStatus.READY: ("#1d4ed8", "#dbeafe"),
    SlotVisualStatus.PROGRAMMING: ("#1d4ed8", "#dbeafe"),
    SlotVisualStatus.READBACK: ("#1d4ed8", "#dbeafe"),
    SlotVisualStatus.RF_READY: ("#1d4ed8", "#dbeafe"),
    SlotVisualStatus.RF_PASS: ("#166534", "#dcfce7"),
    SlotVisualStatus.PRICOL_PASS: ("#166534", "#dcfce7"),
    SlotVisualStatus.FUNCTIONAL_PASS: ("#166534", "#dcfce7"),
    SlotVisualStatus.FUNCTIONAL_FAIL: ("#991b1b", "#fee2e2"),
    SlotVisualStatus.QR_BOUND: ("#166534", "#dcfce7"),
    SlotVisualStatus.HOLD: ("#92400e", "#fef3c7"),
    SlotVisualStatus.UNAVAILABLE: ("#475569", "#cbd5e1"),
}


class JigSlotCard(QFrame):
    """One graphical physical jig position."""

    def __init__(self, number: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.number = number
        self.setObjectName(f"jigSlotCard{number}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(136)

        layout = QVBoxLayout(self)
        layout.setSpacing(5)

        header = QHBoxLayout()

        self.number_label = QLabel(f"Slot {number}")
        self.number_label.setStyleSheet("font-weight: 700; font-size: 13px;")
        header.addWidget(self.number_label)
        header.addStretch()

        self.status_label = QLabel("EMPTY")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.status_label)
        layout.addLayout(header)
        self.stage_label = QLabel("Unoccupied")
        self.stage_label.setWordWrap(True)
        layout.addWidget(self.stage_label)
        self.mac_label = QLabel("No MAC assigned")
        self.mac_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.mac_label)
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName(f"jigSlotProgress{number}")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.setStyleSheet("JigSlotCard {"
            "border: 1px solid #94a3b8;"
            "border-radius: 8px;"
            "}"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        grid = self.parent()
        if isinstance(grid, JigSlotGrid) and event.button() == Qt.MouseButton.LeftButton:
            grid.slot_clicked.emit(self.number)
        super().mousePressEvent(event)

    def render(self, view: SlotView) -> None:
        if view.number != self.number:
            raise ValueError("Slot identity mismatch")
        colour, background = COLOURS[view.status]
        self.status_label.setText(view.status.value)
        self.status_label.setStyleSheet(
            f"color: {colour};"
            f"background-color: {background};"
            "border-radius: 5px;"
            "padding: 4px;"
            "font-weight: bold;"
        )

        self.stage_label.setText(view.stage)
        self.mac_label.setText(view.mac or "No MAC assigned")
        short_detail = view.detail
        if len(short_detail) > 65:
            short_detail = short_detail[:62] + "..."
        self.detail_label.setText(short_detail)
        self.setToolTip(
            f"Slot {view.number}\n"
            f"Status: {view.status.value}\n"
            f"Stage: {view.stage}\n"
            f"MAC: {view.mac or 'Not allocated'}\n"
            f"Detail: {view.detail}"
        )
        if view.progress is None:
            self.progress_bar.hide()
        else:
            if not 0 <= view.progress <= 100:
                raise ValueError("Invalid slot progress")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(view.progress)
            self.progress_bar.show()


class JigSlotGrid(QWidget):
    """Physical 4-up or 8-up graphical jig layout."""

    slot_clicked = pyqtSignal(int)

    def __init__(self, slot_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if slot_count not in (4, 8):
            raise ValueError("Only 4-up or 8-up supported")
        self.slot_count = slot_count
        self.cards: dict[int, JigSlotCard] = {}
        layout = QVBoxLayout(self)
        self.summary_label = QLabel("No active production batch")
        layout.addWidget(self.summary_label)
        grid = QGridLayout()
        grid.setSpacing(8)
        layout.addLayout(grid)
        columns = 4 if slot_count == 8 else 2
        for number in range(1, slot_count + 1):
            card = JigSlotCard(number, self)
            self.cards[number] = card
            grid.addWidget(card, (number - 1) // columns, (number - 1) % columns)
        self.clear_batch()

    def clear_batch(self) -> None:
        self.summary_label.setText("No active production batch")

        for number, card in self.cards.items():
            card.render(
                SlotView(
                    number=number,
                    status=SlotVisualStatus.EMPTY,
                    stage="No active batch",
                )
            )

    def show_slots(self, views: list[SlotView], summary: str) -> None:
        """Render slot views outside a batch (e.g. loading the jig)."""
        self.summary_label.setText(summary)
        for slot in views:
            self.cards[slot.number].render(slot)

    def update_view(self, view: JigView) -> None:
        expected = set(self.cards)
        supplied = [slot.number for slot in view.slots]

        if (len(supplied) != self.slot_count or set(supplied) != expected or len(set(supplied)) != len(supplied)):
            raise ValueError("Jig view must contain every physical slot once")

        self.summary_label.setText(
            f"Batch: {view.batch_id} | "
            f"Occupied: {view.occupied_count}/{self.slot_count} | "
            f"RF PASS: {view.rf_pass_count} | "
            f"HOLD: {view.held_count} | "
            f"Stage: {view.batch_state}"
        )

        for slot in view.slots:
            self.cards[slot.number].render(slot)

