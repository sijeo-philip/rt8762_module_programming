"""Always-visible strip: session state, Operator and Supervisor logins."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget

from stationapp.services.access import StationAccessService
from stationapp.ui.dialogs.access_dialogs import OperatorLoginDialog, SupervisorLoginDialog

GREEN = "#166534"
RED = "#991b1b"
AMBER = "#92400e"
GREY = "#64748b"


class SessionBar(QFrame):
    _changed = pyqtSignal()

    def __init__(self, access: StationAccessService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._access = access
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self.session_label = QLabel()
        self.operator_label = QLabel()
        self.supervisor_label = QLabel()
        for label in (self.session_label, self.operator_label, self.supervisor_label):
            label.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(label)
            layout.addSpacing(18)
        layout.addStretch()

        self.operator_button = QPushButton()
        self.operator_button.clicked.connect(self._toggle_operator)
        layout.addWidget(self.operator_button)

        self.supervisor_button = QPushButton()
        self.supervisor_button.clicked.connect(self._toggle_supervisor)
        layout.addWidget(self.supervisor_button)

        self._changed.connect(self.render, Qt.ConnectionType.QueuedConnection)
        access.add_listener(self._changed.emit)

        # Supervisor elevation expires with inactivity; keep the label honest.
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.render)
        self._timer.start()
        self.render()

    def render(self) -> None:
        access = self._access
        session = access.session
        if session is None:
            self.session_label.setText(f"<b>Session:</b> <span style='color:{RED}'>none - Supervisor must start one</span>")
        else:
            started = session.started_at.astimezone().strftime("%d-%b %H:%M")
            self.session_label.setText(f"<b>Session:</b> <span style='color:{GREEN}'>OPEN</span> since {started} ({session.supervisor_id})")

        operator = access.operator
        if operator is None:
            colour = GREY if session is None else AMBER
            self.operator_label.setText(f"<b>Operator:</b> <span style='color:{colour}'>not logged in</span>")
        else:
            self.operator_label.setText(f"<b>Operator:</b> {operator.label}")

        supervisor = access.supervisor
        self.supervisor_label.setText(
            f"<b>Supervisor:</b> {supervisor.label}" if supervisor else f"<b>Supervisor:</b> <span style='color:{GREY}'>-</span>"
        )

        self.operator_button.setText("Operator logout" if operator else "Operator login")
        self.operator_button.setEnabled(operator is not None or session is not None)
        self.supervisor_button.setText("Supervisor logout" if supervisor else "Supervisor login")

    def _toggle_operator(self) -> None:
        if self._access.operator is not None:
            self._access.logout_operator()
            return
        OperatorLoginDialog(self._access, self).exec()

    def _toggle_supervisor(self) -> None:
        if self._access.supervisor is not None:
            self._access.logout_supervisor()
            return
        dialog = SupervisorLoginDialog(self._access, self)
        if dialog.exec() and self._access.session is None:
            QMessageBox.information(
                self,
                "No production session",
                "Open Station configuration -> Session to review the jig and Golden Rig and start a session.",
            )
