"""Login, first-run and authorization dialogs.

Dialogs only collect input and call the access services; every rule
(roles, lockout, session state) is enforced in StationAccessService.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stationapp.domain.users import AccessError, AuthenticatedUser, PinChangeRequired
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
            self._show_error(str(exc))
            return
        self.accept()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()

    def _submit(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


def _pin_field() -> QLineEdit:
    field = _password_field()
    field.setMaxLength(6)
    return field


class _PinDialog(_CredentialDialog):
    """Phone number + 6-digit PIN, checked by the box.

    A first login with a PIN issued at head office makes the box ask for
    a new PIN: the dialog then shows New PIN / Repeat new PIN, the box
    changes the PIN, and from then on the box checks the new one.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._pending_pin: str | None = None   # PIN typed when the box asked for a new one
        self._form = QFormLayout()
        self.user_input = QLineEdit()
        self.password_input = _pin_field()
        self.password_input.returnPressed.connect(self._submit)
        self._form.addRow("Phone number", self.user_input)
        self._form.addRow("PIN", self.password_input)

        self.new_pin_frame = QWidget()
        new_form = QFormLayout(self.new_pin_frame)
        new_form.setContentsMargins(0, 0, 0, 0)
        self.new_pin_input = _pin_field()
        self.confirm_pin_input = _pin_field()
        self.confirm_pin_input.returnPressed.connect(self._submit)
        new_form.addRow("New PIN", self.new_pin_input)
        new_form.addRow("Repeat new PIN", self.confirm_pin_input)
        self.new_pin_frame.hide()

    def _add_credentials(self) -> None:
        self._layout.addLayout(self._form)
        self._layout.addWidget(self.new_pin_frame)

    def _verify(self, phone: str, pin: str, new_pin: str | None) -> AuthenticatedUser:  # pragma: no cover - overridden
        raise NotImplementedError

    def _submit(self) -> None:
        phone = self.user_input.text()
        self.error_label.hide()
        if self._pending_pin is not None:
            new_pin, confirm = self.new_pin_input.text(), self.confirm_pin_input.text()
            self.new_pin_input.clear()
            self.confirm_pin_input.clear()
            if new_pin != confirm:
                self._show_error("The new PINs do not match.")
                self.new_pin_input.setFocus()
                return
            current = self._pending_pin
            self._attempt(lambda: self._verify(phone, current, new_pin))
            return
        pin = self.password_input.text()
        self.password_input.clear()
        try:
            self.result_user = self._verify(phone, pin, None)
        except PinChangeRequired as exc:
            self._pending_pin = pin
            self.new_pin_frame.show()
            self.user_input.setReadOnly(True)
            self.password_input.setEnabled(False)
            self._show_error(str(exc))
            self.new_pin_input.setFocus()
            return
        except (AccessError, ValueError) as exc:
            self._show_error(str(exc))
            return
        self.accept()


_TAKES_BOX_SESSION = (
    "The box keeps one person logged in per station: after this Supervisor "
    "login the Operator must log in again."
)


class SupervisorLoginDialog(_PinDialog):
    def __init__(self, access: StationAccessService, parent: QWidget | None = None, *, title: str = "Supervisor login") -> None:
        super().__init__(title, parent)
        self._access = access
        self._add_credentials()
        if access.operator is not None and getattr(access.authentication, "one_person_per_station", False):
            note = QLabel(_TAKES_BOX_SESSION)
            note.setWordWrap(True)
            note.setStyleSheet("color: #92400e;")
            self._layout.addWidget(note)
        self._finish(QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel))

    def _verify(self, phone: str, pin: str, new_pin: str | None) -> AuthenticatedUser:
        return self._access.login_supervisor(phone, pin, new_pin=new_pin)


class SupervisorAuthorizationDialog(SupervisorLoginDialog):
    """One-off approval that does not change who is logged in."""

    def __init__(self, access: StationAccessService, action: str, parent: QWidget | None = None) -> None:
        super().__init__(access, parent, title="Supervisor authorization")
        note = QLabel(f"Supervisor authorization is required to: {action}")
        note.setWordWrap(True)
        self._layout.insertWidget(0, note)

    def _verify(self, phone: str, pin: str, new_pin: str | None) -> AuthenticatedUser:
        return self._access.authorize_supervisor(phone, pin, new_pin=new_pin)


class OperatorLoginDialog(_PinDialog):
    """Operator phone number + PIN, verified by the box."""

    def __init__(self, access: StationAccessService, parent: QWidget | None = None) -> None:
        super().__init__("Operator login", parent)
        self._access = access
        self.pin_input = self.password_input
        self._add_credentials()
        self._finish(QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel))
        self.user_input.setFocus()

    def _verify(self, phone: str, pin: str, new_pin: str | None) -> AuthenticatedUser:
        return self._access.login_operator(phone, pin, new_pin=new_pin)


class ClearHoldDialog(QDialog):
    """The hold screen. The line is stopped - by the box, because a QR was
    scanned again, or because a Batch Code was not accepted - and a
    Supervisor types their own phone number and PIN at the station to clear
    the hold (R16). The box checks the PIN.

    blocking: opened by the station itself when the hold arrives; it has no
    Cancel and cannot be closed until the hold is cleared.
    """

    def __init__(self, reports, parent: QWidget | None = None, *, blocking: bool = False) -> None:
        super().__init__(parent)
        self._reports = reports
        self._blocking = blocking
        hold = reports.hold()
        self._bad = hold.bad_batch_ids
        self.setWindowTitle(hold.title)
        self.setModal(True)
        self.setMinimumWidth(720)
        if blocking:
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
            self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)

        banner = QLabel(f"STATION ON HOLD - {hold.title.upper()}")
        banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner.setStyleSheet("background: #b3261e; color: white; font-size: 20px; font-weight: 800; padding: 12px; border-radius: 4px;")
        layout.addWidget(banner)
        operator_note = QLabel("Operator: stop work and call the Supervisor. Nothing can continue until the hold is cleared.")
        operator_note.setWordWrap(True)
        operator_note.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(operator_note)

        reason = QLabel(f"<b>Reason:</b> {hold.reason or 'The station is on hold.'}")
        reason.setWordWrap(True)
        layout.addWidget(reason)

        if hold.reused_qrs:
            layout.addWidget(QLabel("QR scanned again - this label is already on a module made at this station:"))
            table = QTableWidget(len(hold.reused_qrs), 2)
            table.setHorizontalHeaderLabels(["Module QR", "Already bound at"])
            for row, item in enumerate(hold.reused_qrs):
                for column, text in enumerate((item.qr, item.previous)):
                    table.setItem(row, column, QTableWidgetItem(text))
            table.resizeColumnsToContents()
            table.setMaximumHeight(140)
            layout.addWidget(table)
            advice = QLabel(
                "Check the module. If it is a re-worked module, clear the hold and let the operator scan "
                "the same QR again: it is then linked to the module's new MAC. If the label is wrong, "
                "clear the hold, put the right label on the module and scan that one."
            )
            advice.setWordWrap(True)
            layout.addWidget(advice)

        if hold.rejected:
            layout.addWidget(QLabel("Rejected modules - take them out and keep them apart:"))
            table = QTableWidget(len(hold.rejected), 3)
            table.setHorizontalHeaderLabels(["Module QR", "MAC", "Why"])
            for row, item in enumerate(hold.rejected):
                for column, text in enumerate((item.qr, item.mac, item.words)):
                    table.setItem(row, column, QTableWidgetItem(text))
            table.resizeColumnsToContents()
            table.setMaximumHeight(180)
            layout.addWidget(table)

        self.batch_table: QTableWidget | None = None
        if self._bad:
            layout.addWidget(QLabel(
                "<b>Batch Code not accepted by the box.</b> These finished modules cannot be reported until "
                "their Batch Code is corrected. Type the correct Batch Code in the last column "
                "(1 to 64 letters, digits, spaces or punctuation, e.g. S5A01)."
            ))
            fill_row = QHBoxLayout()
            self.fill_input = QLineEdit()
            self.fill_input.setPlaceholderText("Same Batch Code for every module below")
            fill_button = QPushButton("Fill all")
            fill_button.clicked.connect(self._fill_all)
            fill_row.addWidget(self.fill_input, 1)
            fill_row.addWidget(fill_button)
            layout.addLayout(fill_row)

            table = QTableWidget(len(self._bad), 6)
            table.setHorizontalHeaderLabels(["Tray", "Slot", "Module QR", "MAC", "Batch Code given", "Corrected Batch Code"])
            for row, item in enumerate(self._bad):
                given = "(none)" if item.batch_code is None else repr(item.batch_code)
                for column, text in enumerate((item.tray, str(item.slot), item.qr, item.mac, given)):
                    cell = QTableWidgetItem(text)
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    cell.setToolTip(item.problem)
                    table.setItem(row, column, cell)
                table.setItem(row, 5, QTableWidgetItem(""))
            table.resizeColumnsToContents()
            table.horizontalHeader().setStretchLastSection(True)
            table.setMaximumHeight(220)
            layout.addWidget(table)
            self.batch_table = table

        layout.addWidget(QLabel(
            "Supervisor: correct the Batch Codes, then type your own phone number and PIN to clear the hold."
            if self._bad else "Supervisor: type your own phone number and PIN to clear the hold."
        ))
        form = QFormLayout()
        self.phone_input = QLineEdit()
        self.pin_input = _pin_field()
        self.pin_input.returnPressed.connect(self._submit)
        form.addRow("Phone number", self.phone_input)
        form.addRow("PIN", self.pin_input)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #991b1b; font-weight: 600;")
        layout.addWidget(self.error_label)

        if blocking:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        else:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Correct Batch Codes and clear hold" if self._bad else "Clear hold")
        buttons.accepted.connect(self._submit)
        layout.addWidget(buttons)
        (self.batch_table if self._bad else self.phone_input).setFocus()

    def _fill_all(self) -> None:
        if self.batch_table is None:
            return
        text = self.fill_input.text()
        for row in range(self.batch_table.rowCount()):
            self.batch_table.setItem(row, 5, QTableWidgetItem(text))

    def corrections(self) -> dict[tuple[int, int], str]:
        if self.batch_table is None:
            return {}
        result = {}
        for row, item in enumerate(self._bad):
            cell = self.batch_table.item(row, 5)
            result[item.key] = cell.text() if cell is not None else ""
        return result

    def _submit(self) -> None:
        pin = self.pin_input.text()
        self.pin_input.clear()
        try:
            self._reports.clear_hold(self.phone_input.text(), pin, self.corrections())
        except (AccessError, ValueError) as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()

    def reject(self) -> None:
        # Esc or the window's close: the station's own hold screen stays.
        if not self._blocking:
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._blocking and self.result() != QDialog.DialogCode.Accepted:
            event.ignore()
            return
        super().closeEvent(event)


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
