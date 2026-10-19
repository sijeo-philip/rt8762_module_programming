"""Station configuration (Supervisor only).

Session        start / re-confirm / close the production session
Jig slots      bind detected USB serial interfaces to physical slots
Golden Rig     discover, identify, approve and disable the rig binding
Users          add operators/supervisors, reset PINs, badges, disable

The whole screen is disabled unless a Supervisor is logged in; the
services re-check that on every call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timezone

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stationapp.concurrency.manager import OperationManager
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AccessError, UserRole
from stationapp.services.access import StationAccessService, UserAdministrationService
from stationapp.services.golden_rig_configuration import (
    GoldenRigConfigurationService,
    GoldenRigIdentity,
    GoldenRigIdentityError,
    RigEndpoint,
)
from stationapp.services.jig_configuration import JigConfigurationService
from stationapp.services.server_sync import SyncScheduler
from stationapp.ui.dialogs.access_dialogs import ask_reason

logger = logging.getLogger(__name__)

GREEN = "#166534"
RED = "#991b1b"
GREY = "#64748b"

_EXPECTED_ERRORS = (AccessError, GoldenRigIdentityError, ValueError, RuntimeError)


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    header = table.horizontalHeader()
    for column in range(len(headers) - 1):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    return table


def _fill(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            table.setItem(row, column, item)


def _short(fingerprint: str | None) -> str:
    if not fingerprint:
        return "-"
    return fingerprint if len(fingerprint) <= 16 else f"{fingerprint[:8]}...{fingerprint[-8:]}"


class _Page(QWidget):
    """Shared error/notice line and guarded service calls."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.layout_ = QVBoxLayout(self)
        self.message = QLabel("")
        self.message.setWordWrap(True)

    def finish(self) -> None:
        self.layout_.addWidget(self.message)
        self.layout_.addStretch()

    def notice(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {RED if error else GREEN}; font-weight: 600;")

    def guarded(self, action: Callable[[], object], success: str | None = None) -> bool:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            action()
        except _EXPECTED_ERRORS as exc:
            self.notice(str(exc), error=True)
            return False
        except Exception as exc:  # noqa: BLE001 - shown, logged
            logger.exception("Station configuration action failed")
            self.notice(f"Unexpected error: {exc}", error=True)
            return False
        finally:
            QApplication.restoreOverrideCursor()
        if success:
            self.notice(success)
        return True

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionPage(_Page):
    def __init__(self, access: StationAccessService, topology_service=None, parent: QWidget | None = None, *, sync_scheduler: SyncScheduler | None = None) -> None:
        super().__init__(parent)
        self._access = access
        self._sync = sync_scheduler
        self._topology = topology_service
        self._reviewed: StationConfiguration | None = None

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 14px; font-weight: 700;")
        self.layout_.addWidget(self.status_label)

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet(f"color: {RED}; font-weight: 600;")
        self.layout_.addWidget(self.problem_label)

        box = QGroupBox("Configuration to confirm")
        box_layout = QVBoxLayout(box)
        self.config_table = _table(["Item", "Confirmed / live value", "Detected now"])
        box_layout.addWidget(self.config_table)
        self.confirm_jig = QCheckBox("I have identified the jig and confirm the COM-port-to-slot bindings")
        self.confirm_rig = QCheckBox("I confirm the approved Golden Rig binding")
        for check in (self.confirm_jig, self.confirm_rig):
            check.toggled.connect(self._update_buttons)
            box_layout.addWidget(check)
        self.layout_.addWidget(box)

        buttons = QHBoxLayout()
        self.review_button = QPushButton("Review configuration")
        self.review_button.clicked.connect(self.refresh)
        self.start_button = QPushButton("Start production session")
        self.start_button.clicked.connect(self._start)
        self.reconfirm_button = QPushButton("Re-confirm changed configuration")
        self.reconfirm_button.clicked.connect(self._reconfirm)
        self.close_button = QPushButton("Close session")
        self.close_button.clicked.connect(self._close)
        for button in (self.review_button, self.start_button, self.reconfirm_button, self.close_button):
            buttons.addWidget(button)
        buttons.addStretch()
        self.layout_.addLayout(buttons)
        self.finish()

    def refresh(self) -> None:
        access = self._access
        session = access.session
        try:
            live = access.current_configuration()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load station configuration")
            self.notice(f"Could not load station configuration: {exc}", error=True)
            live = None

        detected = self._detected_slots()
        shown = session.configuration if session is not None else live
        self._reviewed = live if session is None else None

        rows: list[list[str]] = []
        if shown is not None:
            rows.append(["Jig", f"{shown.jig_id} ({shown.jig_positions} slots)", ""])
            bound = dict(shown.slot_bindings)
            for slot in range(1, shown.jig_positions + 1):
                key = bound.get(slot)
                state = "-" if key is None else ("simulated" if detected is None else ("yes" if key in detected else "NOT DETECTED"))
                rows.append([f"Slot {slot}", key or "unbound", state])
            rows.append(["Golden Rig", shown.golden_rig_id or "NOT BOUND", ""])
            rows.append(["Rig certificate", _short(shown.golden_rig_fingerprint), ""])
        _fill(self.config_table, rows)

        if session is None:
            self.status_label.setText("No production session is open")
            self.status_label.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {RED};")
            self.problem_label.setText("")
        else:
            started = session.started_at.astimezone().strftime("%d-%b-%Y %H:%M")
            self.status_label.setText(f"Session OPEN since {started}, started by {session.supervisor_id}")
            self.status_label.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {GREEN};")
            problems = access.configuration_problems()
            self.problem_label.setText("\n".join(problems))

        self.confirm_jig.setChecked(False)
        self.confirm_rig.setChecked(False)
        self._update_buttons()

    def _detected_slots(self) -> set[str] | None:
        if self._topology is None:
            return None
        try:
            topology = self._topology.refresh()
        except Exception:  # noqa: BLE001
            logger.exception("Topology refresh failed")
            return set()
        return {slot.stable_key for slot in topology.slots if slot.ready and slot.stable_key}

    def _update_buttons(self) -> None:
        session = self._access.session
        confirmed = self.confirm_jig.isChecked() and self.confirm_rig.isChecked()
        self.confirm_jig.setEnabled(session is None)
        self.confirm_rig.setEnabled(session is None)
        self.start_button.setEnabled(session is None and confirmed and self._reviewed is not None)
        self.reconfirm_button.setEnabled(session is not None and bool(self._access.configuration_problems()))
        self.close_button.setEnabled(session is not None)

    def _start(self) -> None:
        reviewed = self._reviewed
        if reviewed is None:
            return
        if self.guarded(lambda: self._access.start_session(reviewed), "Production session started. Operators may now log in."):
            self.refresh()

    def _reconfirm(self) -> None:
        reason = ask_reason(self, "Re-confirm configuration")
        if reason and self.guarded(lambda: self._access.reconfirm_configuration(reason), "Configuration re-confirmed for this session."):
            self.refresh()

    def _close(self) -> None:
        reason = ask_reason(self, "Close production session", "Reason for closing (e.g. shift end):")
        if reason is None:
            return
        answer = QMessageBox.question(
            self,
            "Close session",
            "Close the production session? The Operator is logged out and no batch can start until a new session is opened.\n\n"
            "Upload of completed batches continues to be queued locally.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        if not self._sync_before_close():
            return
        if self.guarded(lambda: self._access.close_session(reason), "Session closed."):
            self.refresh()

    def _sync_before_close(self) -> bool:
        """README: synchronize with the LAN server before closing."""
        if self._sync is None or not self._sync.service.configured:
            return True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self._sync.sync_now("SESSION_CLOSE")
        finally:
            QApplication.restoreOverrideCursor()
        if report.success:
            self.notice(report.detail)
            return True
        answer = QMessageBox.question(
            self,
            "Server sync failed",
            f"Could not synchronize with the LAN server:\n{report.detail}\n\n"
            "Close the session anyway? All records stay queued and upload automatically later.",
        )
        return answer is QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------------------
# Jig slot binding
# ---------------------------------------------------------------------------


class SlotBindingPage(_Page):
    def __init__(self, service: JigConfigurationService, jig_positions: int, *, simulation: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._ports = ()

        if simulation:
            note = QLabel("SIMULATION MODE: the jig is simulated; these bindings are not used until simulation is switched off.")
            note.setWordWrap(True)
            note.setStyleSheet("color: #92400e; font-weight: 600;")
            self.layout_.addWidget(note)

        self.binding_table = _table(["Slot", "Bound USB identity", "Currently detected as"])
        self.layout_.addWidget(self.binding_table)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Slot"))
        self.slot_spin = QSpinBox()
        self.slot_spin.setRange(1, jig_positions)
        controls.addWidget(self.slot_spin)
        controls.addWidget(QLabel("Detected interface"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(380)
        controls.addWidget(self.port_combo, 1)
        self.bind_button = QPushButton("Bind")
        self.bind_button.clicked.connect(self._bind)
        self.unbind_button = QPushButton("Unbind slot")
        self.unbind_button.clicked.connect(self._unbind)
        self.rescan_button = QPushButton("Rescan ports")
        self.rescan_button.clicked.connect(self.refresh)
        for button in (self.bind_button, self.unbind_button, self.rescan_button):
            controls.addWidget(button)
        self.layout_.addLayout(controls)

        hint = QLabel("Tip: connect one slot's adapter at a time, rescan, then bind it to its physical slot.")
        hint.setStyleSheet(f"color: {GREY};")
        self.layout_.addWidget(hint)
        self.finish()

    def refresh(self) -> None:
        try:
            configuration = self._service.load()
            self._ports = self._service.detected_ports()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load slot bindings")
            self.notice(f"Could not load slot bindings: {exc}", error=True)
            return

        by_key = {port.stable_key: port for port in self._ports}
        rows = []
        for slot in range(1, configuration.jig_positions + 1):
            binding = configuration.get(slot)
            if binding is None:
                rows.append([str(slot), "unbound", "-"])
            else:
                port = by_key.get(binding.stable_key)
                rows.append([str(slot), binding.stable_key, port.device if port else "NOT DETECTED"])
        _fill(self.binding_table, rows)

        bound_keys = {binding.stable_key: binding.slot_number for binding in configuration.bindings}
        self.port_combo.clear()
        for port in self._ports:
            owner = bound_keys.get(port.stable_key)
            suffix = f"  [slot {owner}]" if owner else ""
            self.port_combo.addItem(f"{port.device} - {port.description or ''} - {port.stable_key}{suffix}", port.stable_key)
        self.bind_button.setEnabled(bool(self._ports))

    def _bind(self) -> None:
        key = self.port_combo.currentData()
        port = next((item for item in self._ports if item.stable_key == key), None)
        if port is None:
            return
        slot = self.slot_spin.value()
        reason = ask_reason(self, f"Bind slot {slot}")
        if reason and self.guarded(lambda: self._service.bind(slot_number=slot, port=port, reason=reason), f"Slot {slot} bound to {port.device}."):
            self.refresh()

    def _unbind(self) -> None:
        slot = self.slot_spin.value()
        reason = ask_reason(self, f"Unbind slot {slot}")
        if reason and self.guarded(lambda: self._service.unbind(slot_number=slot, reason=reason), f"Slot {slot} unbound."):
            self.refresh()


# ---------------------------------------------------------------------------
# Golden Rig binding
# ---------------------------------------------------------------------------


class GoldenRigPage(_Page):
    def __init__(self, service: GoldenRigConfigurationService, operations: OperationManager, *, simulation: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._operations = operations
        self._identity: GoldenRigIdentity | None = None
        self._discovered: list = []

        if simulation:
            note = QLabel("SIMULATION MODE: RF tests use the simulated rig; a binding made here applies once simulation is switched off.")
            note.setWordWrap(True)
            note.setStyleSheet("color: #92400e; font-weight: 600;")
            self.layout_.addWidget(note)

        current = QGroupBox("Approved binding")
        current_layout = QFormLayout(current)
        self.current_rig = QLabel("-")
        self.current_endpoint = QLabel("-")
        self.current_fingerprint = QLabel("-")
        self.current_fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.current_approval = QLabel("-")
        current_layout.addRow("Rig ID", self.current_rig)
        current_layout.addRow("Address", self.current_endpoint)
        current_layout.addRow("Certificate SHA-256", self.current_fingerprint)
        current_layout.addRow("Approved", self.current_approval)
        self.disable_button = QPushButton("Disable binding")
        self.disable_button.clicked.connect(self._disable)
        current_layout.addRow("", self.disable_button)
        self.layout_.addWidget(current)

        bind = QGroupBox("Bind a Golden Rig")
        bind_layout = QVBoxLayout(bind)
        discover_row = QHBoxLayout()
        self.discover_button = QPushButton("Discover rigs")
        self.discover_button.clicked.connect(self._discover)
        discover_row.addWidget(self.discover_button)
        inventory = ", ".join(sorted(service.inventory)) or "any rig signed by the station CA"
        discover_row.addWidget(QLabel(f"Approved inventory: {inventory}"))
        discover_row.addStretch()
        bind_layout.addLayout(discover_row)

        self.discovery_table = _table(["Address", "Rig ID", "In inventory", "Certificate", "Result"])
        self.discovery_table.setMaximumHeight(140)
        self.discovery_table.itemSelectionChanged.connect(self._select_discovered)
        bind_layout.addWidget(self.discovery_table)

        entry = QHBoxLayout()
        entry.addWidget(QLabel("mDNS name or IP"))
        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("e.g. golden-rig-01.local or 192.168.10.50")
        self.host_input.setToolTip(
            "The Golden Rig's mDNS name (recommended: survives IP changes) or its IP address. "
            "It must match a name/IP in the rig's certificate."
        )
        entry.addWidget(self.host_input, 1)
        entry.addWidget(QLabel("Port"))
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(8762)
        entry.addWidget(self.port_input)
        self.identity_button = QPushButton("Request identity")
        self.identity_button.clicked.connect(self._request_identity)
        entry.addWidget(self.identity_button)
        bind_layout.addLayout(entry)

        self.identity_label = QLabel("No identity requested")
        self.identity_label.setWordWrap(True)
        self.identity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bind_layout.addWidget(self.identity_label)

        self.approve_button = QPushButton("Approve station-to-rig binding")
        self.approve_button.setEnabled(False)
        self.approve_button.clicked.connect(self._approve)
        bind_layout.addWidget(self.approve_button)
        self.layout_.addWidget(bind)

        history = QGroupBox("Binding audit history")
        history_layout = QVBoxLayout(history)
        self.history_table = _table(["When", "Supervisor", "Action", "Rig", "Reason"])
        self.history_table.setMaximumHeight(150)
        history_layout.addWidget(self.history_table)
        self.layout_.addWidget(history)
        self.finish()

    def refresh(self) -> None:
        try:
            binding = self._service.current_binding()
            history = self._service.history()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load Golden Rig binding")
            self.notice(f"Could not load Golden Rig binding: {exc}", error=True)
            return

        if binding is None:
            self.current_rig.setText("NOT BOUND")
            self.current_rig.setStyleSheet(f"color: {RED}; font-weight: 700;")
            for label in (self.current_endpoint, self.current_fingerprint, self.current_approval):
                label.setText("-")
        else:
            state = "" if binding.enabled else "  (DISABLED)"
            self.current_rig.setText(binding.rig_id + state)
            self.current_rig.setStyleSheet(f"color: {GREEN if binding.enabled else RED}; font-weight: 700;")
            self.current_endpoint.setText(f"{binding.host}:{binding.port}")
            self.current_fingerprint.setText(binding.certificate_sha256)
            self.current_approval.setText(f"{binding.approved_by} at {binding.approved_at.astimezone():%d-%b-%Y %H:%M}")
        self.disable_button.setEnabled(binding is not None and binding.enabled)

        rows = []
        for record in reversed(history[-50:]):
            rows.append([
                str(record.get("created_at", ""))[:19],
                str(record.get("user_id", "")),
                str(record.get("action", "")),
                str(record.get("record_id", "")),
                str(record.get("reason", "")),
            ])
        _fill(self.history_table, rows)

    # Probes may take seconds per rig: run them off the GUI thread.
    def _background(self, operation_id: str, work: Callable[[], object], done: Callable[[object], None], busy: list[QPushButton]) -> None:
        def operation(_id, _token, _progress):
            try:
                return work()
            except _EXPECTED_ERRORS as exc:
                return exc

        def finished(value: object) -> None:
            if isinstance(value, Exception):
                self.notice(str(value), error=True)
            else:
                done(value)

        def configure(worker) -> None:
            worker.succeeded.connect(lambda result: finished(result.value))
            worker.failed.connect(lambda failure: self.notice(failure.user_message, error=True))
            worker.finished.connect(lambda _id: [button.setEnabled(True) for button in busy])

        try:
            self._operations.start(operation, operation_id=operation_id, configure=configure)
        except Exception as exc:  # noqa: BLE001
            self.notice(str(exc), error=True)
            return
        for button in busy:
            button.setEnabled(False)
        self.notice("Contacting Golden Rig...")

    def _discover(self) -> None:
        self._background("golden-rig-discover", self._service.discover, self._show_discovery, [self.discover_button])

    def _show_discovery(self, results) -> None:
        self._discovered = list(results)
        rows = []
        for result in results:
            identity = result.identity
            if identity is None:
                rows.append([str(result.endpoint), "-", "-", "-", result.error or "failed"])
            else:
                rows.append([identity.endpoint_label, identity.rig_id, "yes" if identity.in_inventory else "NO", _short(identity.certificate_sha256), "trusted"])
        _fill(self.discovery_table, rows)
        self.notice(f"{len(results)} candidate(s) probed." if results else "No candidate rigs configured; enter a host/IP.")

    def _select_discovered(self) -> None:
        rows = self.discovery_table.selectionModel().selectedRows()
        if not rows:
            return
        result = self._discovered[rows[0].row()]
        self.host_input.setText(result.endpoint.host)
        self.port_input.setValue(result.endpoint.port)
        if result.identity is not None:
            self._show_identity(result.identity)

    def _request_identity(self) -> None:
        try:
            endpoint = RigEndpoint(self.host_input.text(), self.port_input.value())
        except ValueError as exc:
            self.notice(str(exc), error=True)
            return
        self._identity = None
        self.approve_button.setEnabled(False)
        self._background("golden-rig-identity", lambda: self._service.request_identity(endpoint), self._show_identity, [self.identity_button])

    def _show_identity(self, identity: GoldenRigIdentity) -> None:
        self._identity = identity
        inventory = "in approved inventory" if identity.in_inventory else "NOT in approved inventory"
        self.identity_label.setText(
            f"<b>{identity.rig_id}</b> at {identity.endpoint_label} - certificate trusted by station CA "
            f"({identity.issuer}), valid until {identity.valid_until}, {inventory}.<br>"
            f"SHA-256: {identity.certificate_sha256}"
        )
        self.identity_label.setStyleSheet(f"color: {GREEN if identity.approvable else RED};")
        self.approve_button.setEnabled(identity.approvable)
        self.notice("Identity verified. Review it, then approve the binding." if identity.approvable else "This rig cannot be approved.", error=not identity.approvable)

    def _approve(self) -> None:
        identity = self._identity
        if identity is None:
            return
        answer = QMessageBox.question(
            self,
            "Approve Golden Rig",
            f"Bind this station to Golden Rig {identity.rig_id} at {identity.endpoint}?\n\n"
            "Only this rig, presenting exactly this certificate, will be used for Stock RF tests.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        reason = ask_reason(self, "Approve Golden Rig binding")
        if reason and self.guarded(lambda: self._service.approve(identity, reason=reason), f"Golden Rig {identity.rig_id} approved for this station."):
            self.approve_button.setEnabled(False)
            self.refresh()

    def _disable(self) -> None:
        reason = ask_reason(self, "Disable Golden Rig binding")
        if reason and self.guarded(lambda: self._service.disable(reason=reason), "Golden Rig binding disabled. Stock RF testing is blocked until a rig is bound."):
            self.refresh()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class AddUserDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add user")
        form = QFormLayout(self)
        self.user_input = QLineEdit()
        self.name_input = QLineEdit()
        self.role_combo = QComboBox()
        self.role_combo.addItem("Operator", UserRole.OPERATOR)
        self.role_combo.addItem("Supervisor", UserRole.SUPERVISOR)
        self.secret_input = QLineEdit()
        self.secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.badge_input = QLineEdit()
        self.badge_input.setPlaceholderText("Optional: scan badge (Operators only use badge login)")
        form.addRow("User ID", self.user_input)
        form.addRow("Full name", self.name_input)
        form.addRow("Role", self.role_combo)
        form.addRow("PIN / password", self.secret_input)
        form.addRow("Badge", self.badge_input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class UsersPage(_Page):
    def __init__(self, users: UserAdministrationService, access: StationAccessService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._users = users
        self._access = access
        self.user_table = _table(["User ID", "Name", "Role", "Status", "Badge"])
        self.layout_.addWidget(self.user_table)

        buttons = QHBoxLayout()
        actions = (
            ("Add user", self._add),
            ("Reset PIN / password", self._reset),
            ("Set badge", self._badge),
            ("Enable / disable", self._toggle),
        )
        for label, handler in actions:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch()
        self.layout_.addLayout(buttons)
        self.finish()

    def refresh(self) -> None:
        try:
            users = self._users.list_users()
        except Exception as exc:  # noqa: BLE001
            self.notice(f"Could not load users: {exc}", error=True)
            return
        self._rows = list(users)
        _fill(self.user_table, [
            [user.user_id, user.display_name, user.role.value, "active" if user.active else "DISABLED", "assigned" if user.badge_id else "-"]
            for user in users
        ])

    def _selected(self):
        rows = self.user_table.selectionModel().selectedRows()
        if not rows:
            self.notice("Select a user first", error=True)
            return None
        return self._rows[rows[0].row()]

    def _add(self) -> None:
        dialog = AddUserDialog(self)
        if not dialog.exec():
            return
        reason = ask_reason(self, "Add user") or "New user"
        self.guarded(
            lambda: self._users.create_user(
                self._access.require_supervisor(),
                user_id=dialog.user_input.text(),
                display_name=dialog.name_input.text(),
                role=dialog.role_combo.currentData(),
                secret=dialog.secret_input.text(),
                badge_id=dialog.badge_input.text(),
                reason=reason,
            ),
            "User added.",
        )
        self.refresh()

    def _reset(self) -> None:
        user = self._selected()
        if user is None:
            return
        from PyQt6.QtWidgets import QInputDialog

        secret, ok = QInputDialog.getText(self, "Reset PIN / password", f"New PIN / password for {user.user_id}:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        reason = ask_reason(self, "Reset PIN / password")
        if reason:
            self.guarded(lambda: self._users.reset_secret(self._access.require_supervisor(), user_id=user.user_id, secret=secret, reason=reason), f"Secret reset for {user.user_id}; account unlocked.")

    def _badge(self) -> None:
        user = self._selected()
        if user is None:
            return
        from PyQt6.QtWidgets import QInputDialog

        badge, ok = QInputDialog.getText(self, "Set badge", f"Scan badge for {user.user_id} (empty to remove):", QLineEdit.EchoMode.Password)
        if not ok:
            return
        reason = ask_reason(self, "Set badge")
        if reason and self.guarded(lambda: self._users.set_badge(self._access.require_supervisor(), user_id=user.user_id, badge_id=badge, reason=reason), "Badge updated."):
            self.refresh()

    def _toggle(self) -> None:
        user = self._selected()
        if user is None:
            return
        reason = ask_reason(self, ("Disable " if user.active else "Enable ") + user.user_id)
        if reason and self.guarded(lambda: self._users.set_active(self._access.require_supervisor(), user_id=user.user_id, active=not user.active, reason=reason), "User updated."):
            self.refresh()


# ---------------------------------------------------------------------------
# LAN server sync
# ---------------------------------------------------------------------------


class SyncPage(_Page):
    def __init__(self, scheduler: SyncScheduler, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scheduler = scheduler

        status_box = QGroupBox("Synchronization status")
        form = QFormLayout(status_box)
        self.server_label = QLabel()
        self.pending_label = QLabel()
        self.history_label = QLabel()
        self.qr_label = QLabel()
        self.last_label = QLabel()
        self.last_label.setWordWrap(True)
        form.addRow("LAN server", self.server_label)
        form.addRow("Waiting to upload", self.pending_label)
        form.addRow("History not yet sent", self.history_label)
        form.addRow("Module QR", self.qr_label)
        form.addRow("Last sync", self.last_label)
        self.sync_button = QPushButton("Sync now")
        self.sync_button.clicked.connect(self._sync_now)
        form.addRow("", self.sync_button)
        self.layout_.addWidget(status_box)

        runs = QGroupBox("Recent sync runs")
        runs_layout = QVBoxLayout(runs)
        self.runs_table = _table(["Started", "Trigger", "Result", "Modules", "Batches", "Events", "Detail"])
        self.runs_table.setMaximumHeight(200)
        runs_layout.addWidget(self.runs_table)
        self.layout_.addWidget(runs)

        conflicts = QGroupBox("Duplicate QR conflicts reported by the LAN server")
        conflicts_layout = QVBoxLayout(conflicts)
        self.conflict_table = _table(["Module QR", "Batch", "Slot", "Also bound at"])
        self.conflict_table.setMaximumHeight(160)
        conflicts_layout.addWidget(self.conflict_table)
        self.layout_.addWidget(conflicts)
        self.finish()

        # Scheduler callbacks arrive on the sync thread; a queued signal
        # brings the refresh onto the GUI thread.
        self._changed.connect(self._refresh_if_visible, Qt.ConnectionType.QueuedConnection)
        scheduler.add_listener(self._changed.emit)

    _changed = pyqtSignal()

    def _refresh_if_visible(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        service = self._scheduler.service
        try:
            status = service.status()
            conflicts = service.conflicts()
            from stationapp.data.sync_repository import SyncRepository

            with service._session_factory() as session:  # noqa: SLF001 - read-only view
                runs = [
                    [
                        (run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)).astimezone().strftime("%d-%b %H:%M:%S"),
                        run.trigger,
                        run.status,
                        str(run.modules),
                        str(run.batches),
                        str(run.events),
                        run.detail or "",
                    ]
                    for run in SyncRepository(session).last_runs(20)
                ]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not read sync status")
            self.notice(f"Could not read sync status: {exc}", error=True)
            return

        self.server_label.setText("configured" if status.configured else "NOT CONFIGURED - set LAN_SERVER_URL")
        self.pending_label.setText(
            f"{status.pending_modules} modules in {status.pending_batches} batches "
            f"(automatic sync at {status.threshold})"
        )
        self.history_label.setText(f"{status.unsynced_events} events, {status.unsynced_audits} audit records")
        self.qr_label.setText(f"{status.unconfirmed_qr} not yet confirmed by server, {status.qr_conflicts} conflict(s)")
        last = status.last_success_at.astimezone().strftime("%d-%b-%Y %H:%M") if status.last_success_at else "never"
        self.last_label.setText(f"Last success: {last}" + (f" | Last error: {status.last_error}" if status.last_error else ""))
        self.sync_button.setEnabled(status.configured and not status.running)
        _fill(self.runs_table, runs)
        _fill(self.conflict_table, [
            [item["module_qr"], item["batch_id"] or "-", str(item["slot_number"] or "-"), item["detail"] or ""]
            for item in conflicts
        ])

    def _sync_now(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self._scheduler.sync_now("MANUAL")
        finally:
            QApplication.restoreOverrideCursor()
        self.notice(report.detail, error=not report.success)
        self.refresh()


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


class StationConfigurationWidget(QWidget):
    _changed = pyqtSignal()

    def __init__(self, *, access: StationAccessService, users: UserAdministrationService | None, jig_configuration: JigConfigurationService, golden_rig: GoldenRigConfigurationService, operations: OperationManager, jig_positions: int, topology_service=None, simulation: bool = False, sync_scheduler: SyncScheduler | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._access = access
        layout = QVBoxLayout(self)

        self.locked_label = QLabel("Supervisor login required to change the station configuration.")
        self.locked_label.setStyleSheet(f"color: {RED}; font-weight: 700; font-size: 14px;")
        layout.addWidget(self.locked_label)

        self.tabs = QTabWidget()
        self.session_page = SessionPage(access, None if simulation else topology_service, sync_scheduler=sync_scheduler)
        self.slot_page = SlotBindingPage(jig_configuration, jig_positions, simulation=simulation)
        self.rig_page = GoldenRigPage(golden_rig, operations, simulation=simulation)
        if users is not None:
            self.users_page = UsersPage(users, access)
        else:
            self.users_page = QLabel(
                "Operators and Supervisors are enrolled on the LAN server. "
                "Every login on this station is verified there, so any enrolled user "
                "can log in on any jig."
            )
            self.users_page.setWordWrap(True)
            self.users_page.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self.users_page.setContentsMargins(12, 12, 12, 12)
        self.tabs.addTab(self.session_page, "Session")
        self.tabs.addTab(self.slot_page, "Jig slot binding")
        self.tabs.addTab(self.rig_page, "Golden Rig binding")
        self.tabs.addTab(self.users_page, "Users")
        self.sync_page: SyncPage | None = None
        if sync_scheduler is not None:
            self.sync_page = SyncPage(sync_scheduler)
            self.tabs.addTab(self.sync_page, "Server sync")
        self.tabs.currentChanged.connect(lambda _index: self._refresh_current())
        layout.addWidget(self.tabs)

        self._changed.connect(self.render, Qt.ConnectionType.QueuedConnection)
        access.add_listener(self._changed.emit)
        self.render()

    def render(self) -> None:
        unlocked = self._access.supervisor is not None
        self.locked_label.setVisible(not unlocked)
        self.tabs.setEnabled(unlocked)
        if unlocked:
            self._refresh_current()

    def _refresh_current(self) -> None:
        page = self.tabs.currentWidget()
        if isinstance(page, _Page) and self._access.supervisor is not None:
            page.refresh()
