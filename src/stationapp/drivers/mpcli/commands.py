"""Validated command builders for Realtek MPCLI."""

from __future__ import annotations

import re
from pathlib import Path

from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.configuration import (MpCliProgrammingProfile)
from stationapp.drivers.mpcli.errors import (MpCliInvalidCommand)


_COM_PORT = re.compile(
    r"^COM([1-9][0-9]*)$",
    re.IGNORECASE,
)


def build_version_command() -> tuple[str, ...]:
    """Query MPCLI version."""

    return ("-V",)


def normalize_com_port(com_port: str) -> str:
    """Return canonical Windows COM port name."""

    value = com_port.strip().upper()

    if not _COM_PORT.fullmatch(value):
        raise MpCliInvalidCommand(f"Invalid Windows COM port: {com_port!r}")
    return value


def build_flash_with_mac_command( *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile) -> tuple[str, ...]:
    """Build one packed-image programming command.
    The operation:
        selects COM port,
        selects baud,
        provides packed firmware image,
        writes the requested MAC,
        provides the configured Product ID,
        provides the configured Secret Key,
        optionally reboots after programming.

    This builder performs no subprocess execution.
    """
    port = normalize_com_port(com_port)
    image = _path_text(profile.image_packet)
    command: list[str] = ["-P",image, "-c", port, "-b", str(profile.baud), "-x", mac.compact, "-n", profile.product_id, "-k", profile.secret_key]
    if profile.reboot_after_programming:
        command.append("-r")
    return tuple(command)

def build_read_mac_command(*, com_port: str, baud: int = 1_000_000) -> tuple[str, ...]:
    """Build MPCLI EUID/MAC read-back command."""
    port = normalize_com_port(com_port)
    _validate_baud(baud) 
    return ("-c", port, "-b", str(baud), "-I")


def build_reboot_command(*, com_port: str, baud: int = 1_000_000) -> tuple[str, ...]:
    """Build standalone device reboot command."""

    port = normalize_com_port(com_port)
    _validate_baud(baud)
    return ("-c", port, "-b", str(baud),"-r")


def _validate_baud(baud: int) -> None:

    if baud not in {1_000_000, 2_000_000, 3_000_000}:
        raise MpCliInvalidCommand(
            "Windows MPCLI baud must be "
            "1000000, 2000000 or 3000000"
        )


def _path_text(value: Path) -> str:

    text = str(value).strip()
    if not text:
        raise MpCliInvalidCommand("image packet path cannot be empty")

    return text

def sanitise_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """Remove secrets before commands enter logs or audit records."""

    result = list(command)
    try:
        index = result.index("-k")

    except ValueError:
        return tuple(result)

    if index + 1 < len(result):
        result[index + 1] = "<REDACTED>"

    return tuple(result)


