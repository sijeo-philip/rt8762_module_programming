"""Station login screen: one login for Operators and Supervisors.

Phone number and 6-digit PIN are verified by the box, which also
returns the person's role. The role then decides which screen is shown.
A first login with a PIN issued at head office asks for a new PIN.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stationapp.domain.users import AccessError, PinChangeRequired
from stationapp.services.access import StationAccessService

RED = "#991b1b"
AMBER = "#92400e"
GREY = "#64748b"


def _pin_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setMaxLength(6)
    field.setMinimumHeight(34)
    return field


class LoginPage(QWidget):
    def __init__(self, access: StationAccessService, *, station_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._access = access
        self._pending_pin: str | None = None   # PIN typed when the box asked for a new one
        outer = QVBoxLayout(self)
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        card = QFrame()
        card.setObjectName("loginCard")
        card.setStyleSheet("#loginCard { border: 1px solid #94a3b8; border-radius: 10px; }")
        card.setMinimumWidth(460)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        title = QLabel(f"Station {station_label}")
        title.setStyleSheet("font-size: 22px; font-weight: 800;")
        layout.addWidget(title)

        auth = access.authentication
        description = getattr(auth, "mode_description", "")
        self.mode_label = QLabel(description)
        self.mode_label.setWordWrap(True)
        if getattr(auth, "test_users", False):
            self.mode_label.setStyleSheet(f"background: {AMBER}; color: white; font-weight: 700; padding: 6px; border-radius: 4px;")
        elif not getattr(auth, "available", True):
            self.mode_label.setStyleSheet(f"color: {RED}; font-weight: 700;")
        else:
            self.mode_label.setStyleSheet(f"color: {GREY};")
        layout.addWidget(self.mode_label)

        form = QFormLayout()
        self.user_input = QLineEdit()
        self.user_input.setMinimumHeight(34)
        self.user_input.returnPressed.connect(self.password_input_focus)
        self.password_input = _pin_field()
        self.password_input.returnPressed.connect(self._login)
        form.addRow("Phone number", self.user_input)
        form.addRow("PIN", self.password_input)
        layout.addLayout(form)

        # Shown when the box asks for a new PIN.
        self.new_pin_frame = QWidget()
        new_form = QFormLayout(self.new_pin_frame)
        new_form.setContentsMargins(0, 0, 0, 0)
        self.new_pin_input = _pin_field()
        self.confirm_pin_input = _pin_field()
        self.confirm_pin_input.returnPressed.connect(self._login)
        new_form.addRow("New PIN", self.new_pin_input)
        new_form.addRow("Repeat new PIN", self.confirm_pin_input)
        self.new_pin_frame.hide()
        layout.addWidget(self.new_pin_frame)

        self.login_button = QPushButton("LOG IN")
        self.login_button.setMinimumHeight(44)
        self.login_button.setStyleSheet(
            "QPushButton { font-size: 15px; font-weight: 700; color: white; background: #1d4ed8; border-radius: 6px; }"
        )
        self.login_button.clicked.connect(self._login)
        layout.addWidget(self.login_button)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {RED}; font-weight: 600;")
        layout.addWidget(self.error_label)

        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)

    def reset(self) -> None:
        for field in (self.user_input, self.password_input, self.new_pin_input, self.confirm_pin_input):
            field.clear()
        self._set_new_pin_mode(False)
        self.error_label.clear()
        self.user_input.setFocus()

    def password_input_focus(self) -> None:
        self.password_input.setFocus()

    def _login(self) -> None:
        phone = self.user_input.text()
        self.error_label.clear()
        if self._pending_pin is not None:
            new_pin, confirm = self.new_pin_input.text(), self.confirm_pin_input.text()
            self.new_pin_input.clear()
            self.confirm_pin_input.clear()
            if new_pin != confirm:
                self.error_label.setText("The new PINs do not match.")
                return
            current = self._pending_pin
            self._attempt(lambda: self._access.login_with_new_pin(phone, current, new_pin))
            return
        pin = self.password_input.text()
        self.password_input.clear()
        try:
            self._access.login(phone, pin)
        except PinChangeRequired as exc:
            self._pending_pin = pin
            self._set_new_pin_mode(True)
            self.error_label.setText(str(exc))
            self.new_pin_input.setFocus()
            return
        except (AccessError, ValueError) as exc:
            self.error_label.setText(str(exc))
            return
        self.reset()

    def _set_new_pin_mode(self, on: bool) -> None:
        if not on:
            self._pending_pin = None
        self.new_pin_frame.setVisible(on)
        self.user_input.setReadOnly(on)
        self.password_input.setEnabled(not on)
        self.login_button.setText("SET NEW PIN AND LOG IN" if on else "LOG IN")

    def _attempt(self, action) -> None:
        try:
            action()
        except (AccessError, ValueError) as exc:
            self.error_label.setText(str(exc))
            return
        self.reset()
