"""Serial-port enumeration for the station."""

from __future__ import annotations
from collections.abc import Iterable
from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo

from stationapp.drivers.serial.types import (DiscoveredSerialPort, UsbSerialIdentity)


def _convert_port(port: ListPortInfo) -> DiscoveredSerialPort:

    identity = UsbSerialIdentity(vid=port.vid, pid=port.pid, serial_number=port.serial_number, location=port.location)
    return DiscoveredSerialPort(device=port.device, identity=identity, description=port.description, manufacturer=port.manufacturer, product=port.product, hwid=port.hwid)


def discover_serial_ports() -> tuple[DiscoveredSerialPort, ...]:
    """Enumerate serial ports currently visible to the OS."""

    ports = (_convert_port(port) for port in list_ports.comports())
    return tuple(sorted(ports,
            key=lambda item: item.device,
        ),
    )


def discover_stable_usb_ports() -> tuple[DiscoveredSerialPort, ...]:
    """Return only ports suitable for persistent jig binding."""

    discovered: list[DiscoveredSerialPort] = []
    for port in discover_serial_ports():
        try:
            port.stable_key
        except ValueError:
            continue
        discovered.append(port)
    return tuple(discovered)


def index_by_stable_key( ports: Iterable[DiscoveredSerialPort]) -> dict[str, DiscoveredSerialPort]:
    """Index discovered interfaces by persistent USB identity.

    Duplicate identities are considered unsafe because the station could
    otherwise program the wrong physical DUT.
    """

    result: dict[str, DiscoveredSerialPort] = {}
    for port in ports:
        key = port.stable_key
        if key in result:
            raise ValueError(f"Duplicate serial identity detected: {key}: {result[key].device} and {port.device}")
        result[key] = port
    return result

