"""Supervisor page: programming mode, firmware, lot and enabled slots."""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from stationapp.domain.batch import ProgrammingMode
from stationapp.domain.users import AccessError
from stationapp.services.production_settings import ProductionSettings, ProductionSettingsService
from stationapp.ui.dialogs.access_dialogs import ask_reason

logger = logging.getLogger(__name__)

GREEN = "#166534"
RED = "#991b1b"
GREY = "#64748b"


class ProductionSettingsPage(QWidget):
    _changed = pyqtSignal()

    def __init__(self, service: ProductionSettingsService, *, jig_configuration=None, simulation: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._jig = jig_configuration
        self._simulation = simulation
        layout = QVBoxLayout(self)

        mode_box = QGroupBox("Programming mode")
        mode_layout = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.stock_only = QRadioButton("Stock only")
        self.stock_pricol = QRadioButton("Stock + Pricol")
        for button in (self.stock_only, self.stock_pricol):
            self.mode_group.addButton(button)
            mode_layout.addWidget(button)
            button.toggled.connect(self._update_enabled)
        mode_layout.addStretch()
        layout.addWidget(mode_box)

        firmware_box = QGroupBox("Firmware images")
        form = QFormLayout(firmware_box)
        self.stock_input = self._file_row(form, "Stock firmware")
        self.pricol_input = self._file_row(form, "Pricol firmware")
        self.lot_input = QLineEdit()
        self.lot_input.setPlaceholderText("Lot / batch identification recorded with every batch")
        form.addRow("Lot", self.lot_input)
        layout.addWidget(firmware_box)

        slots_box = QGroupBox("Enabled jig slots")
        self.slot_grid = QGridLayout(slots_box)
        self.slot_checks: dict[int, QCheckBox] = {}
        self.slot_ports: dict[int, QLabel] = {}
        for number in range(1, service.jig_positions + 1):
            check = QCheckBox(f"Slot {number}")
            port = QLabel("-")
            port.setStyleSheet(f"color: {GREY};")
            row = number - 1
            self.slot_grid.addWidget(check, row, 0)
            self.slot_grid.addWidget(port, row, 1)
            self.slot_checks[number] = check
            self.slot_ports[number] = port
        self.slot_grid.setColumnStretch(1, 1)
        layout.addWidget(slots_box)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save settings")
        self.save_button.clicked.connect(self._save)
        self.revert_button = QPushButton("Discard changes")
        self.revert_button.clicked.connect(self.refresh)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.revert_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch()

        self._changed.connect(self.refresh, Qt.ConnectionType.QueuedConnection)
        service.add_listener(self._changed.emit)
        self.refresh()

    def _file_row(self, form: QFormLayout, label: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit()
        browse = QPushButton("Browse...")

        def choose() -> None:
            path, _ = QFileDialog.getOpenFileName(self, label, edit.text(), "Firmware (*.bin);;All files (*)")
            if path:
                edit.setText(path)

        browse.clicked.connect(choose)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        form.addRow(label, row)
        return edit

    def refresh(self) -> None:
        settings = self._service.current()
        self.stock_only.setChecked(settings.mode is ProgrammingMode.STOCK_ONLY)
        self.stock_pricol.setChecked(settings.mode is ProgrammingMode.STOCK_AND_PRICOL)
        self.stock_input.setText(str(settings.stock_firmware or ""))
        self.pricol_input.setText(str(settings.pricol_firmware or ""))
        self.lot_input.setText(settings.lot or "")
        for number, check in self.slot_checks.items():
            check.setChecked(number in settings.enabled_slots)
        self._show_ports()
        self._update_enabled()

    def _show_ports(self) -> None:
        if self._simulation:
            for number, label in self.slot_ports.items():
                label.setText(f"COM{100 + number} (simulated)")
            return
        if self._jig is None:
            return
        try:
            bindings = self._jig.load()
            detected = {port.stable_key: port.device for port in self._jig.detected_ports()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read slot bindings: %s", exc)
            return
        for number, label in self.slot_ports.items():
            binding = bindings.get(number)
            if binding is None:
                label.setText("not bound - bind it in Station configuration")
                label.setStyleSheet(f"color: {RED};")
            elif binding.stable_key in detected:
                label.setText(f"{detected[binding.stable_key]}  ({binding.stable_key})")
                label.setStyleSheet(f"color: {GREEN};")
            else:
                label.setText(f"NOT DETECTED  ({binding.stable_key})")
                label.setStyleSheet(f"color: {RED};")

    def _update_enabled(self) -> None:
        self.pricol_input.setEnabled(self.stock_pricol.isChecked())

    def _save(self) -> None:
        mode = ProgrammingMode.STOCK_ONLY if self.stock_only.isChecked() else ProgrammingMode.STOCK_AND_PRICOL
        stock = self.stock_input.text().strip()
        pricol = self.pricol_input.text().strip()
        settings = ProductionSettings(
            mode=mode,
            stock_firmware=Path(stock) if stock else None,
            pricol_firmware=Path(pricol) if pricol and mode is ProgrammingMode.STOCK_AND_PRICOL else None,
            enabled_slots=tuple(number for number, check in self.slot_checks.items() if check.isChecked()),
            lot=self.lot_input.text().strip() or None,
        )
        for label, path in (("Stock", settings.stock_firmware), ("Pricol", settings.pricol_firmware)):
            if path is not None and not path.is_file():
                self._notice(f"{label} firmware file not found: {path}", error=True)
                return
        reason = ask_reason(self, "Save production settings")
        if reason is None:
            return
        try:
            self._service.update(settings, reason=reason)
        except (AccessError, ValueError) as exc:
            self._notice(str(exc), error=True)
            return
        self._notice("Production settings saved. Operators use them from the next batch.")

    def _notice(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {RED if error else GREEN}; font-weight: 600;")
