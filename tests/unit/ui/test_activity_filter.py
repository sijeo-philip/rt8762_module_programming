"""Real input at the station resets the inactivity timer."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QPushButton

from stationapp.ui.main_window import ActivityFilter


class FakeAccess:
    def __init__(self, lapsed: bool = False) -> None:
        self.lapsed = lapsed
        self.calls = 0

    def note_activity(self) -> bool:
        self.calls += 1
        return self.lapsed


def key_press() -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")


def test_input_on_a_widget_is_activity_and_passes_through(qtbot):
    button = QPushButton()
    qtbot.addWidget(button)
    access = FakeAccess()

    assert ActivityFilter(access).eventFilter(button, key_press()) is False
    assert access.calls == 1


def test_input_that_ends_lapsed_logins_is_dropped(qtbot):
    button = QPushButton()
    qtbot.addWidget(button)

    assert ActivityFilter(FakeAccess(lapsed=True)).eventFilter(button, key_press()) is True


def test_other_events_and_non_widgets_are_not_activity(qtbot):
    button = QPushButton()
    qtbot.addWidget(button)
    access = FakeAccess()
    activity = ActivityFilter(access)

    assert activity.eventFilter(button, QEvent(QEvent.Type.Paint)) is False
    assert activity.eventFilter(QObject(), key_press()) is False
    assert access.calls == 0
