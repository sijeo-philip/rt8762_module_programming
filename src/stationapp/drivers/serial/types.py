"""Typed serial-port identity models.

A Windows COM port name is transient.  The station therefore identifies a
physical serial interface from USB metadata and resolves that identity to the
current COM port during discovery.
"""

from __future__ import annotations
from dataclasses import dataclass


def _clean(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()

    return value if value else None


@dataclass(frozen=True, slots=True)
class UsbSerialIdentity:
    """USB identity independent of the assigned Windows COM number."""

    vid: int | None
    pid: int | None
    serial_number: str | None
    location: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "serial_number", _clean(self.serial_number))
        object.__setattr__(self, "location", _clean(self.location))

    @property
    def vid_hex(self) -> str | None:
        if self.vid is None:
            return None

        return f"{self.vid:04X}"

    @property
    def pid_hex(self) -> str | None:
        if self.pid is None:
            return None

        return f"{self.pid:04X}"

    @property
    def has_usb_identity(self) -> bool:
        return self.vid is not None and self.pid is not None

    @property
    def stable_key(self) -> str:
        """Return the best stable identity available.

        Preference:

            USB serial number
                    ↓
            physical USB/hub location
                    ↓
            insufficient identity

        COM number is deliberately never part of this key.
        """

        if self.vid is None or self.pid is None:
            raise ValueError(
                "Serial interface has no USB VID/PID and cannot "
                "be used as a stable jig identity."
            )

        prefix = f"{self.vid:04X}:{self.pid:04X}"

        if self.serial_number:
            return (
                f"USB:{prefix}:SERIAL:"
                f"{self.serial_number.upper()}"
            )

        if self.location:
            return (
                f"USB:{prefix}:LOCATION:"
                f"{self.location.upper()}"
            )

        raise ValueError(
            "USB serial interface has neither a serial number "
            "nor a physical USB location."
        )


@dataclass(frozen=True, slots=True)
class DiscoveredSerialPort:
    """One serial interface discovered on the station PC."""

    device: str
    identity: UsbSerialIdentity
    description: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    hwid: str | None = None

    def __post_init__(self) -> None:
        device = self.device.strip()
        if not device:
            raise ValueError("Serial device name cannot be empty.")

        object.__setattr__(self, "device", device.upper())

    @property
    def stable_key(self) -> str:
        return self.identity.stable_key


    