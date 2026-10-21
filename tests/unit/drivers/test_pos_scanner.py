"""HID POS scanner: label decoding, device choice, hand-off to the scan field."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from stationapp.drivers.pos_scanner import choose_scanner, decode_label
from stationapp.drivers.qr_scanner import BADGE_FORMAT, MODULE_QR_FORMAT
from stationapp.ui.widgets.scanner_input import ScannerLineEdit

# Device list as Windows reported it on the bench PC.
HONEYWELL = ("1470g", r"\?\HID#VID_0C2E&PID_1001&MI_01#9&211fcd68&0&0000#{c243ffbd-3afc-45e9-b3d3-2ba18bc7ebc5}\POSBarcodeScanner")
CAMERA = (
    "Microsoft BarcodeScanner (Integrated Camera)",
    r"POS#{FA655F3D-FB77-4538-B04C-29566B8452E4}_{FF2F86FF}_\?\USB$VID_174F&PID_2459&MI_00$7&6c047c5&1&0000\GLOBAL",
)


@pytest.mark.unit
def test_decode_label_strips_terminators():
    assert decode_label(b"Unisem0A00000B\r\n") == "Unisem0A00000B"
    assert decode_label(b"Unisem0A00000B\x00") == "Unisem0A00000B"


@pytest.mark.unit
def test_usb_scanner_is_chosen_never_the_camera():
    assert choose_scanner([CAMERA, HONEYWELL]) == HONEYWELL
    assert choose_scanner([CAMERA]) is None
    assert choose_scanner([]) is None


class FakeBridge(QObject):
    scanned = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    status = "HID POS scanner: 1470g connected"

    def stop(self):
        pass


@pytest.mark.unit
def test_hid_pos_read_goes_through_the_same_qr_rules(qtbot):
    field = ScannerLineEdit(MODULE_QR_FORMAT, debounce_ms=2000)
    qtbot.addWidget(field)
    got, rejected, repeated = [], [], []
    field.scanned.connect(got.append)
    field.rejected.connect(rejected.append)
    field.repeated.connect(repeated.append)

    field.submit("Unisem0A00000B")       # the label on the bench modules
    field.submit("Unisem0A00000B")       # held trigger / double read
    field.submit("Unisem")               # not enough hex characters

    assert got == ["0A00000B"]
    assert repeated == ["0A00000B"]
    assert rejected == ["Unisem"]


@pytest.mark.unit
def test_badge_read_by_hid_pos(qtbot):
    field = ScannerLineEdit(BADGE_FORMAT, idle_complete=True)
    qtbot.addWidget(field)
    got = []
    field.scanned.connect(got.append)
    field.submit("OPBADGE001\r\n")
    assert got == ["OPBADGE001"]
