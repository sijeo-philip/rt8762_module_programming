"""Settings shared by every test."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_pos_scanner(monkeypatch):
    # Never claim a real USB scanner plugged into the PC running the tests.
    monkeypatch.setenv("QR_SCANNER_HID_POS", "false")
