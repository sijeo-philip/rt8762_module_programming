from serial.tools.list_ports_common import ListPortInfo
from stationapp.drivers.serial.discovery import (_convert_port, index_by_stable_key)


def make_port(device: str, *, vid: int | None, pid: int | None, serial_number: str | None, location: str | None) -> ListPortInfo:
    port = ListPortInfo(device)
    port.vid = vid
    port.pid = pid
    port.serial_number = serial_number
    port.location = location
    port.description = "Test USB Serial"
    port.manufacturer = "Test Manufacturer"
    port.product = "USB UART"
    port.hwid = "TEST"
    return port


def test_pyserial_port_is_converted():
    raw = make_port(
        "COM8",
        vid=0x10C4,
        pid=0xEA60,
        serial_number="DEVICE001",
        location="1-3.1",
    )
    port = _convert_port(raw)
    assert port.device == "COM8"
    assert port.identity.vid == 0x10C4
    assert port.identity.pid == 0xEA60
    assert port.identity.serial_number == "DEVICE001"
    assert port.identity.location == "1-3.1"


def test_ports_can_be_indexed_by_identity():
    first = _convert_port(
        make_port(
            "COM4",
            vid=0x1234,
            pid=0x0001,
            serial_number="PORT1",
            location="1-1",
        )
    )

    second = _convert_port(
        make_port(
            "COM9",
            vid=0x1234,
            pid=0x0001,
            serial_number="PORT2",
            location="1-2",
        )
    )

    index = index_by_stable_key(
        [first, second]
    )
    assert index[first.stable_key].device == "COM4"
    assert index[second.stable_key].device == "COM9"

import pytest


def test_duplicate_stable_identity_is_rejected():
    first = _convert_port(
        make_port(
            "COM5",
            vid=0x1234,
            pid=0x5678,
            serial_number="SAME",
            location="1-1",
        )
    )

    second = _convert_port(
        make_port(
            "COM6",
            vid=0x1234,
            pid=0x5678,
            serial_number="SAME",
            location="1-2",
        )
    )

    with pytest.raises(
        ValueError,
        match="Duplicate serial identity",
    ):
        index_by_stable_key(
            [first, second]
        )

  