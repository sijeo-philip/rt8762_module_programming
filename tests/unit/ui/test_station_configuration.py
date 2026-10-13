"""Supervisor screens: session start, Golden Rig binding, operator login."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from PyQt6.QtWidgets import QMessageBox

from stationapp.concurrency.manager import OperationManager
from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import UserRole
from stationapp.infrastructure.golden_rig.sqlite_binding_store import SqliteGoldenRigBindingStore
from stationapp.services.access import AuthenticationService, SecretHasher, StationAccessService, UserAdministrationService
from stationapp.services.golden_rig_configuration import (
    GoldenRigConfigurationService,
    GoldenRigIdentityProbe,
    live_rig_configuration,
)
from stationapp.services.golden_rig_secure_binding import SecureGoldenRigBindingService
from stationapp.services.jig_configuration import JigConfigurationService
from stationapp.services.slot_binding import SlotBindingService
from stationapp.ui.dialogs.access_dialogs import OperatorLoginDialog
from stationapp.ui.widgets import station_configuration as screen
from stationapp.ui.widgets.session_bar import SessionBar
from stationapp.ui.widgets.station_configuration import StationConfigurationWidget
from tests.integration.golden_rig.test_golden_rig_tls import create_test_certificates
from tests.integration.services.test_golden_rig_configuration import HandshakeServer

STATION = "STATION-01"
FAST = SecretHasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture
def ui(qtbot, tmp_path: Path, monkeypatch):
    ca, cert, key, fingerprint = create_test_certificates(tmp_path)
    rig = HandshakeServer(cert, key)

    database_url = f"sqlite:///{(tmp_path / 'station.db').as_posix()}"
    engine = create_station_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    store = SqliteGoldenRigBindingStore(database_url)
    store.create_schema_for_tests()

    def configuration() -> StationConfiguration:
        rig_id, rig_fp = live_rig_configuration(store, STATION)
        return StationConfiguration("JIG-01", 4, ((1, "USB:A"), (2, "USB:B")), rig_id, rig_fp)

    auth = AuthenticationService(session_factory=factory, station_id=STATION, hasher=FAST)
    users = UserAdministrationService(session_factory=factory, station_id=STATION, hasher=FAST)
    users.create_first_supervisor(user_id="sup1", display_name="Asha", password="supervisor-pw")
    users.create_user(auth.authenticate("sup1", "supervisor-pw", purpose="setup"), user_id="op1", display_name="Ravi", role=UserRole.OPERATOR, secret="1234", badge_id="BADGE-77", reason="setup")

    access = StationAccessService(session_factory=factory, station_id=STATION, authentication=auth, configuration_provider=configuration, supervisor_timeout=timedelta(minutes=15))
    golden = GoldenRigConfigurationService(
        station_id=STATION,
        store=store,
        binding_service=SecureGoldenRigBindingService(STATION, store),
        probe=GoldenRigIdentityProbe(ca_certificate=ca, timeout=2.0),
        access=access,
    )
    jig = JigConfigurationService(
        station_id=STATION,
        binding_service=SlotBindingService(binding_file=tmp_path / "bindings.json", station_id=STATION, jig_id="JIG-01", jig_positions=4),
        access=access,
        session_factory=factory,
        discover=lambda: (),
    )
    operations = OperationManager()
    widget = StationConfigurationWidget(
        access=access, users=users, jig_configuration=jig, golden_rig=golden,
        operations=operations, jig_positions=4,
    )
    bar = SessionBar(access)
    qtbot.addWidget(widget)
    qtbot.addWidget(bar)

    # Dialog prompts are answered automatically.
    monkeypatch.setattr(screen, "ask_reason", lambda *args, **kwargs: "UI test")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)

    yield {"widget": widget, "bar": bar, "access": access, "rig": rig, "fingerprint": fingerprint, "golden": golden, "operations": operations}

    operations.cancel_all()
    operations.wait_for_all(5_000)
    rig.close()
    engine.dispose()


@pytest.mark.unit
def test_configuration_locked_until_supervisor_login(ui, qtbot):
    widget, access = ui["widget"], ui["access"]

    assert not widget.tabs.isEnabled()
    assert widget.locked_label.isVisibleTo(widget)

    access.login_supervisor("sup1", "supervisor-pw")
    qtbot.waitUntil(widget.tabs.isEnabled, timeout=2_000)
    assert "Asha" in ui["bar"].supervisor_label.text()


@pytest.mark.unit
def test_bind_rig_then_start_session_then_operator_badge_login(ui, qtbot):
    widget, access = ui["widget"], ui["access"]
    access.login_supervisor("sup1", "supervisor-pw")
    qtbot.waitUntil(widget.tabs.isEnabled, timeout=2_000)

    # Golden Rig binding page: request identity from the live rig, approve.
    widget.tabs.setCurrentWidget(widget.rig_page)
    page = widget.rig_page
    page.host_input.setText("127.0.0.1")
    page.port_input.setValue(ui["rig"].port)
    page.identity_button.click()
    qtbot.waitUntil(page.approve_button.isEnabled, timeout=5_000)
    assert "GOLDEN-RIG-TEST-01" in page.identity_label.text()

    page.approve_button.click()
    assert ui["golden"].current_binding().certificate_sha256 == ui["fingerprint"]
    assert page.current_rig.text() == "GOLDEN-RIG-TEST-01"

    # Session page: both confirmations are required before START.
    widget.tabs.setCurrentWidget(widget.session_page)
    session_page = widget.session_page
    session_page.refresh()
    assert not session_page.start_button.isEnabled()
    session_page.confirm_jig.setChecked(True)
    assert not session_page.start_button.isEnabled()
    session_page.confirm_rig.setChecked(True)
    assert session_page.start_button.isEnabled()

    session_page.start_button.click()
    assert access.session is not None, session_page.message.text()
    qtbot.waitUntil(lambda: "OPEN" in ui["bar"].session_label.text(), timeout=2_000)

    # Operator badge login through the dialog.
    dialog = OperatorLoginDialog(access)
    qtbot.addWidget(dialog)
    qtbot.keyClicks(dialog.badge_input, "BADGE-77")
    dialog._submit()
    assert access.operator.user_id == "OP1"
    qtbot.waitUntil(lambda: "Ravi" in ui["bar"].operator_label.text(), timeout=2_000)


@pytest.mark.unit
def test_operator_login_dialog_shows_refusal(ui, qtbot):
    dialog = OperatorLoginDialog(ui["access"])
    qtbot.addWidget(dialog)
    qtbot.keyClicks(dialog.badge_input, "BADGE-77")
    dialog._submit()

    assert ui["access"].operator is None
    assert "Supervisor must start" in dialog.error_label.text()
