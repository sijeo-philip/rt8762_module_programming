"""Login screen against the demo box: phone + PIN, forced PIN change."""

from __future__ import annotations

import pytest

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.domain.session import StationConfiguration
from stationapp.drivers.box.demo import DemoBox
from stationapp.services.access import StationAccessService
from stationapp.services.box_auth import BoxAuthenticationService
from stationapp.ui.widgets.login_page import LoginPage

SUPERVISOR_PHONE = "9000000003"


@pytest.fixture
def page(qtbot, tmp_path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    box = DemoBox()
    auth = BoxAuthenticationService(gateway=box.gateway(), session_factory=factory, station_id="S", mode_description="demo", test_users=True)
    access = StationAccessService(session_factory=factory, station_id="S", authentication=auth, configuration_provider=lambda: StationConfiguration("J", 4, ((1, "U1"),), "R", "a" * 64))
    widget = LoginPage(access, station_label="S / J")
    qtbot.addWidget(widget)
    yield widget, access, box
    engine.dispose()


def type_login(qtbot, page, phone: str, pin: str) -> None:
    qtbot.keyClicks(page.user_input, phone)
    qtbot.keyClicks(page.password_input, pin)
    qtbot.keyClick(page.password_input, "\r")


@pytest.mark.unit
def test_wrong_pin_is_refused_in_plain_words(page, qtbot):
    widget, access, _ = page
    type_login(qtbot, widget, SUPERVISOR_PHONE, "999999")
    assert access.supervisor is None
    assert "not recognised" in widget.error_label.text()


@pytest.mark.unit
def test_forced_pin_change_then_login(page, qtbot):
    widget, access, box = page
    box.people[SUPERVISOR_PHONE]["must_change"] = True

    type_login(qtbot, widget, SUPERVISOR_PHONE, "246810")
    assert access.supervisor is None
    assert widget.new_pin_frame.isVisibleTo(widget)
    assert "new 6-digit PIN" in widget.error_label.text()

    qtbot.keyClicks(widget.new_pin_input, "112233")
    qtbot.keyClicks(widget.confirm_pin_input, "112234")
    widget.login_button.click()
    assert "do not match" in widget.error_label.text()
    assert access.supervisor is None

    qtbot.keyClicks(widget.new_pin_input, "112233")
    qtbot.keyClicks(widget.confirm_pin_input, "112233")
    widget.login_button.click()

    assert access.supervisor is not None and access.supervisor.user_id == "SUP-1"
    assert box.people[SUPERVISOR_PHONE]["pin"] == "112233"
    assert not widget.new_pin_frame.isVisibleTo(widget)


@pytest.mark.unit
def test_supervisor_authorization_dialog_sets_a_first_time_pin(page, qtbot):
    from stationapp.ui.dialogs.access_dialogs import SupervisorAuthorizationDialog

    _, access, box = page
    box.people[SUPERVISOR_PHONE]["must_change"] = True
    dialog = SupervisorAuthorizationDialog(access, "change the Batch Code")
    qtbot.addWidget(dialog)

    qtbot.keyClicks(dialog.user_input, SUPERVISOR_PHONE)
    qtbot.keyClicks(dialog.password_input, "246810")
    dialog._submit()
    assert dialog.result_user is None
    assert dialog.new_pin_frame.isVisibleTo(dialog)
    assert "new 6-digit PIN" in dialog.error_label.text()

    qtbot.keyClicks(dialog.new_pin_input, "112233")
    qtbot.keyClicks(dialog.confirm_pin_input, "112233")
    dialog._submit()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.result_user is not None and dialog.result_user.is_supervisor
    assert box.people[SUPERVISOR_PHONE]["pin"] == "112233"
