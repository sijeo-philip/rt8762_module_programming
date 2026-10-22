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
        optionally reboots after programming.

    MPCLI's optional -n/-k pair is not sent; -x works without it.
    This builder performs no subprocess execution.
    """
    port = normalize_com_port(com_port)
    image = _path_text(profile.image_packet)
    command: list[str] = ["-P", image, "-c", port, "-b", str(profile.baud), "-x", mac.colon]
    if profile.reboot_after_programming:
        command.append("-r")
    return tuple(command)

def build_flash_command( *, com_port: str, profile: MpCliProgrammingProfile) -> tuple[str, ...]:
    """Build one packed-image programming command.
    The operation:
        selects COM port,
        selects baud,
        provides packed firmware image,
        optionally reboots after programming.
        """
    port = normalize_com_port(com_port)
    image = _path_text(profile.image_packet)
    command = ["-P", image, "-c", port, "-b", str(profile.baud)]
    if profile.reboot_after_programming:
        command.append("-r")
    return tuple(command)

def build_set_mac_command( *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile, reboot: bool = True) -> tuple[str, ...]:
    """Build standalone MAC programming command."""

    port = normalize_com_port(com_port)
    command: list[str] = [
        "-c",
        port,
        "-b",
        str(profile.baud),
        "-x",
        mac.colon,
    ]

    if reboot:
        command.append("-r")

    return tuple(command)

def build_read_mac_command(*, com_port: str, baud: int = 1_000_000, reboot: bool = True) -> tuple[str, ...]:
    """Build MPCLI EUID/MAC read-back command.

    -r resets the module after the read, as the flash and MAC-write
    commands do, so no stage leaves the module running MPCLI's RAM loader
    (verified on the bench: mpcli -c COM8 -I -r).
    """
    port = normalize_com_port(com_port)
    _validate_baud(baud)
    command = ["-c", port, "-b", str(baud), "-I"]
    if reboot:
        command.append("-r")
    return tuple(command)



def build_chip_erase_command(*, com_port: str, baud: int = 1_000_000, keep_first_4k: bool = False) -> tuple[str, ...]:
    """Build the chip erase run before flashing firmware.

    -E erases the whole flash; -C keeps 0x800000-0x801000. Run as its own
    MPCLI call so the erase is certain to happen before -P programs the
    image. -r resets the module afterwards, as every other stage does.
    """
    port = normalize_com_port(com_port)
    _validate_baud(baud)
    return ("-c", port, "-b", str(baud), "-C" if keep_first_4k else "-E", "-r")


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


