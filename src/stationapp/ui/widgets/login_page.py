"""Station login screen: one login for Operators and Supervisors.

Credentials are verified by the LAN server, which also decides the
user's role. The role then decides which screen is shown.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
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

from stationapp.domain.users import AccessError
from stationapp.services.access import StationAccessService

RED = "#991b1b"
AMBER = "#92400e"
GREY = "#64748b"


class LoginPage(QWidget):
    def __init__(self, access: StationAccessService, *, station_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._access = access
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

        self.badge_input = QLineEdit()
        self.badge_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.badge_input.setPlaceholderText("Scan your badge (Operators)")
        self.badge_input.setMinimumHeight(40)
        self.badge_input.returnPressed.connect(self._badge_login)
        layout.addWidget(self.badge_input)

        divider = QLabel("or log in with your user ID")
        divider.setAlignment(Qt.AlignmentFlag.AlignCenter)
        divider.setStyleSheet(f"color: {GREY};")
        layout.addWidget(divider)

        form = QFormLayout()
        self.user_input = QLineEdit()
        self.user_input.setMinimumHeight(34)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setMinimumHeight(34)
        self.password_input.returnPressed.connect(self._password_login)
        form.addRow("User ID", self.user_input)
        form.addRow("Password", self.password_input)
        layout.addLayout(form)

        self.login_button = QPushButton("LOG IN")
        self.login_button.setMinimumHeight(44)
        self.login_button.setStyleSheet(
            "QPushButton { font-size: 15px; font-weight: 700; color: white; background: #1d4ed8; border-radius: 6px; }"
        )
        self.login_button.clicked.connect(self._password_login)
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
        for field in (self.badge_input, self.user_input, self.password_input):
            field.clear()
        self.error_label.clear()
        self.badge_input.setFocus()

    def _password_login(self) -> None:
        user_id, password = self.user_input.text(), self.password_input.text()
        self.password_input.clear()
        self._attempt(lambda: self._access.login(user_id, password))

    def _badge_login(self) -> None:
        badge = self.badge_input.text()
        self.badge_input.clear()
        if badge.strip():
            self._attempt(lambda: self._access.login_with_badge(badge))

    def _attempt(self, action) -> None:
        self.error_label.clear()
        try:
            action()
        except (AccessError, ValueError) as exc:
            self.error_label.setText(str(exc))
            return
        self.reset()
