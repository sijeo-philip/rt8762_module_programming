import pytest

from stationapp.drivers.serial.types import (DiscoveredSerialPort, UsbSerialIdentity)

def test_serial_number_is_preferred_for_stable_identity():
    identity = UsbSerialIdentity(
        vid=0x10C4,
        pid=0xEA60,
        serial_number="abc123",
        location="1-3.2",
    )
    assert identity.stable_key == (
        "USB:10C4:EA60:SERIAL:ABC123"
    )


def test_location_is_used_when_serial_number_missing():
    identity = UsbSerialIdentity(
        vid=0x0403,
        pid=0x6001,
        serial_number=None,
        location="1-4.1",
    )
    assert identity.stable_key == (
        "USB:0403:6001:LOCATION:1-4.1"
    )


def test_com_port_is_not_part_of_stable_identity():
    identity = UsbSerialIdentity(
        vid=0x10C4,
        pid=0xEA60,
        serial_number="adapter-01",
        location="1-2",
    )
    first = DiscoveredSerialPort(
        device="COM7",
        identity=identity,
    )
    second = DiscoveredSerialPort(
        device="COM19",
        identity=identity,
    )

    assert first.stable_key == second.stable_key


def test_missing_usb_identity_is_rejected():
    identity = UsbSerialIdentity(
        vid=None,
        pid=None,
        serial_number=None,
        location=None,
    )
    with pytest.raises(ValueError):
        _ = identity.stable_key


def test_vid_pid_without_serial_or_location_is_rejected():
    identity = UsbSerialIdentity(
        vid=0x1234,
        pid=0x5678,
        serial_number=None,
        location=None,
    )
    with pytest.raises(ValueError):
        _ = identity.stable_key


def test_device_name_is_normalised():
    port = DiscoveredSerialPort(
        device="com17",
        identity=UsbSerialIdentity(
            vid=0x1234,
            pid=0x5678,
            serial_number="ABC",
            location=None,
        ),
    )

    assert port.device == "COM17"