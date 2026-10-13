"""Login, first-run and authorization dialogs.

Dialogs only collect input and call the access services; every rule
(roles, lockout, session state) is enforced in StationAccessService.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stationapp.domain.users import AccessError, AuthenticatedUser
from stationapp.services.access import StationAccessService, UserAdministrationService


class _CredentialDialog(QDialog):
    """Base: shows an inline error and stays open until success/cancel."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(380)
        self.result_user: AuthenticatedUser | None = None
        self._layout = QVBoxLayout(self)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #991b1b; font-weight: 600;")
        self.error_label.hide()

    def _finish(self, buttons: QDialogButtonBox) -> None:
        self._layout.addWidget(self.error_label)
        self._layout.addWidget(buttons)
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)

    def _attempt(self, action: Callable[[], AuthenticatedUser]) -> None:
        try:
            self.result_user = action()
        except (AccessError, ValueError) as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        self.accept()

    def _submit(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


class SupervisorLoginDialog(_CredentialDialog):
    def __init__(self, access: StationAccessService, parent: QWidget | None = None, *, title: str = "Supervisor login") -> None:
        super().__init__(title, parent)
        self._access = access
        form = QFormLayout()
        self.user_input = QLineEdit()
        self.password_input = _password_field()
        self.password_input.returnPressed.connect(self._submit)
        form.addRow("Supervisor ID", self.user_input)
        form.addRow("Password", self.password_input)
        self._layout.addLayout(form)
        self._finish(QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel))

    def _submit(self) -> None:
        self._attempt(lambda: self._access.login_supervisor(self.user_input.text(), self.password_input.text()))
        self.password_input.clear()


class SupervisorAuthorizationDialog(SupervisorLoginDialog):
    """One-off approval that does not change who is logged in."""

    def __init__(self, access: StationAccessService, action: str, parent: QWidget | None = None) -> None:
        super().__init__(access, parent, title="Supervisor authorization")
        note = QLabel(f"Supervisor authorization is required to: {action}")
        note.setWordWrap(True)
        self._layout.insertWidget(0, note)

    def _submit(self) -> None:
        self._attempt(lambda: self._access.authorize_supervisor(self.user_input.text(), self.password_input.text()))
        self.password_input.clear()


class OperatorLoginDialog(_CredentialDialog):
    """Badge scan (default) or operator ID + PIN."""

    def __init__(self, access: StationAccessService, parent: QWidget | None = None) -> None:
        super().__init__("Operator login", parent)
        self._access = access
        self.tabs = QTabWidget()

        badge_page = QWidget()
        badge_form = QFormLayout(badge_page)
        self.badge_input = _password_field()
        self.badge_input.setPlaceholderText("Scan your badge")
        self.badge_input.returnPressed.connect(self._submit)
        badge_form.addRow("Badge", self.badge_input)
        self.tabs.addTab(badge_page, "Badge")

        pin_page = QWidget()
        pin_form = QFormLayout(pin_page)
        self.user_input = QLineEdit()
        self.pin_input = _password_field()
        self.pin_input.returnPressed.connect(self._submit)
        pin_form.addRow("Operator ID", self.user_input)
        pin_form.addRow("PIN / password", self.pin_input)
        self.tabs.addTab(pin_page, "ID + PIN")

        self._layout.addWidget(self.tabs)
        self._finish(QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel))
        self.badge_input.setFocus()

    def _submit(self) -> None:
        if self.tabs.currentIndex() == 0:
            badge = self.badge_input.text()
            self.badge_input.clear()
            self._attempt(lambda: self._access.login_operator_badge(badge))
        else:
            pin = self.pin_input.text()
            self.pin_input.clear()
            self._attempt(lambda: self._access.login_operator(self.user_input.text(), pin))


class FirstSupervisorDialog(QDialog):
    """Initial station setup: create the first Supervisor account."""

    def __init__(self, users: UserAdministrationService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._users = users
        self.setWindowTitle("Initial station setup")
        self.setModal(True)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "No users exist on this station. Create the first Supervisor account. "
            "The Supervisor then adds Operators, binds the jig and the Golden Rig."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.user_input = QLineEdit()
        self.name_input = QLineEdit()
        self.password_input = _password_field()
        self.confirm_input = _password_field()
        form.addRow("Supervisor ID", self.user_input)
        form.addRow("Full name", self.name_input)
        form.addRow("Password (8+ chars)", self.password_input)
        form.addRow("Confirm password", self.confirm_input)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #991b1b; font-weight: 600;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _submit(self) -> None:
        if self.password_input.text() != self.confirm_input.text():
            self.error_label.setText("Passwords do not match")
            return
        try:
            self._users.create_first_supervisor(
                user_id=self.user_input.text(),
                display_name=self.name_input.text(),
                password=self.password_input.text(),
            )
        except (AccessError, ValueError) as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()


def ask_reason(parent: QWidget, title: str, prompt: str = "Reason (recorded in the audit trail):") -> str | None:
    text, ok = QInputDialog.getText(parent, title, prompt)
    if not ok or not text.strip():
        return None
    return text.strip()
