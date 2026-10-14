"""Login screen, Operator view and Supervisor view on the real app context."""

from __future__ import annotations

from pathlib import Path

import pytest

from stationapp.config import get_settings


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
        "LAN_SERVER_URL": "",
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
    context.sync_scheduler.shutdown()
    context.sync_scheduler.wait(5)
    get_settings.cache_clear()


def visible_tabs(window) -> list[str]:
    return [window._tabs.tabText(i) for i in range(window._tabs.count()) if window._tabs.isTabVisible(i)]


@pytest.mark.unit
def test_role_based_screens(app_window, qtbot):
    window, context = app_window
    access = context.access
    login = window._login_page
    panel = window._production_panel

    # Nobody logged in: login page, test users announced.
    assert window._stack.currentWidget() is login
    assert "TEST USERS" in login.mode_label.text()

    # Operator cannot log in before a Supervisor opens a session.
    login.user_input.setText("OPERATOR1")
    login.password_input.setText("op1234")
    login.login_button.click()
    assert "Supervisor must log in" in login.error_label.text()
    assert window._stack.currentWidget() is login

    # Supervisor: full view with every tab and the detailed setup.
    login.user_input.setText("supervisor1")
    login.password_input.setText("Sup@1234")
    login.login_button.click()
    qtbot.waitUntil(lambda: window._stack.currentWidget() is window._work_page, timeout=3_000)
    assert visible_tabs(window) == ["Production", "Production settings", "Station configuration", "Station status"]
    assert window._tabs.tabBar().isVisible() or not window.isVisible()
    assert panel.supervisor_view
    access.start_session(access.current_configuration())

    # Log out -> login page; Operator logs in with the badge.
    window._session_bar.operator_button.click()
    qtbot.waitUntil(lambda: window._stack.currentWidget() is login, timeout=3_000)
    login.badge_input.setText("OPBADGE001")
    login.badge_input.returnPressed.emit()
    qtbot.waitUntil(lambda: window._stack.currentWidget() is window._work_page, timeout=3_000)

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

    # Supervisor elevation from the Operator view, then back.
    access.login("SUPERVISOR1", "Sup@1234")
    qtbot.waitUntil(lambda: panel.supervisor_view, timeout=3_000)
    assert "Production settings" in visible_tabs(window)
    access.logout_current()
    qtbot.waitUntil(lambda: not panel.supervisor_view, timeout=3_000)
    assert access.operator is not None and visible_tabs(window) == ["Production"]
