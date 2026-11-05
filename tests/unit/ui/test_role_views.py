"""Login screen, Operator view and Supervisor view on the real app context."""

from __future__ import annotations

from pathlib import Path

import pytest

from stationapp.config import get_settings

OPERATOR = ("9000000001", "123456")
SUPERVISOR = ("9000000003", "246810")


@pytest.fixture
def app_window(qtbot, tmp_path: Path, monkeypatch):
    for key, value in {
        "STATION_ID": "STN-UI",
        "JIG_ID": "JIG-UI",
        "JIG_POSITIONS": "4",
        "SIMULATION_MODE": "true",
        "SIMULATION_DELAY_SECONDS": "0",
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'station.db').as_posix()}",
        "LOG_DIR": str(tmp_path / "logs"),
        "BOX_URL": "",
        "AUTH_MODE": "server",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    from stationapp.bootstrap import bootstrap
    from stationapp.ui.main_window import MainWindow

    context = bootstrap()
    window = MainWindow(context)
    qtbot.addWidget(window)
    yield window, context
    window.shutdown()
    context.box_reports.shutdown()
    context.box_reports.wait(5)
    get_settings.cache_clear()


def visible_tabs(window) -> list[str]:
    return [window._tabs.tabText(i) for i in range(window._tabs.count()) if window._tabs.isTabVisible(i)]


def log_in(login, phone: str, pin: str) -> None:
    login.user_input.setText(phone)
    login.password_input.setText(pin)
    login.login_button.click()


@pytest.mark.unit
def test_role_based_screens(app_window, qtbot):
    window, context = app_window
    access = context.access
    login = window._login_page
    panel = window._production_panel

    # Nobody logged in: login page, demo box announced.
    assert window._stack.currentWidget() is login
    assert "DEMO BOX" in login.mode_label.text()

    # Operator cannot log in before a Supervisor opens a session.
    log_in(login, *OPERATOR)
    assert "Supervisor must log in" in login.error_label.text()
    assert window._stack.currentWidget() is login

    # Supervisor (role from the box): full view with every tab.
    log_in(login, *SUPERVISOR)
    qtbot.waitUntil(lambda: window._stack.currentWidget() is window._work_page, timeout=3_000)
    assert visible_tabs(window) == ["Production", "Production settings", "Station configuration", "Station status"]
    assert window._tabs.tabBar().isVisible() or not window.isVisible()
    assert panel.supervisor_view
    access.start_session(access.current_configuration(), batch_code_threshold=100)

    # Log out -> login page; Operator logs in with phone and PIN.
    window._session_bar.operator_button.click()
    qtbot.waitUntil(lambda: window._stack.currentWidget() is login, timeout=3_000)
    log_in(login, *OPERATOR)
    qtbot.waitUntil(lambda: window._stack.currentWidget() is window._work_page, timeout=3_000)

    # First Operator of the session: the Batch Code series comes first.
    qtbot.waitUntil(lambda: panel.headline_label.text() == "ENTER BATCH CODE", timeout=10_000)
    assert not panel.primary_button.isEnabled() and not panel.batch_code_button.isHidden()
    context.batch_codes.start_series("S5A01")

    # Operator: Production only, no settings, compact header.
    assert visible_tabs(window) == ["Production"]
    assert not panel.supervisor_view
    assert panel.setup_box.isHidden()
    assert window._session_bar._compact
    assert not window._session_bar.supervisor_button.isHidden()

    # The jig becomes READY on its own; slots are toggled by tapping cards.
    qtbot.waitUntil(lambda: panel.headline_label.text().startswith("JIG READY"), timeout=10_000)
    assert panel.selected_slots == (1, 2, 3, 4)
    window._jig_slot_grid.slot_clicked.emit(2)
    assert panel.selected_slots == (1, 3, 4)
    assert window._jig_slot_grid.cards[2].status_label.text() == "EMPTY"

    # Supervisor elevation from the Operator view: the box keeps one
    # person per station, so the Operator must log in again afterwards.
    access.login(*SUPERVISOR)
    qtbot.waitUntil(lambda: panel.supervisor_view, timeout=3_000)
    assert "Production settings" in visible_tabs(window)
    assert access.operator is None
    access.logout_current()
    qtbot.waitUntil(lambda: window._stack.currentWidget() is login, timeout=3_000)
    log_in(login, *OPERATOR)
    qtbot.waitUntil(lambda: access.operator is not None and visible_tabs(window) == ["Production"], timeout=3_000)


@pytest.mark.unit
def test_hid_pos_scan_on_login_page_is_ignored(app_window, qtbot):
    from tests.unit.drivers.test_pos_scanner import FakeBridge

    window, context = app_window
    bridge = FakeBridge()
    window.attach_pos_scanner(bridge)
    bridge.scanned.emit("OPBADGE001")                           # no badge login any more
    assert window._stack.currentWidget() is window._login_page
    assert context.access.operator is None and context.access.supervisor is None
