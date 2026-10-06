from pathlib import Path

import pytest

from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.commands import (
    build_flash_with_mac_command,
    build_read_mac_command,
    build_reboot_command,
    build_version_command,
    normalize_com_port,
    sanitise_command,
)
from stationapp.drivers.mpcli.configuration import (
    MpCliProgrammingProfile,
)
from stationapp.drivers.mpcli.errors import (
    MpCliInvalidCommand,
)


VALID_KEY = (
    "00112233445566778899AABBCCDDEEFF"
    "00112233445566778899AABBCCDDEEFF"
)


def make_profile(
    *,
    reboot: bool = True,
) -> MpCliProgrammingProfile:

    return MpCliProgrammingProfile(
        image_packet=Path(
            r"C:\firmware\stock.bin"
        ),
        product_id="11223344",
        secret_key=VALID_KEY,
        reboot_after_programming=reboot,
    )


def test_version_command() -> None:

    assert build_version_command() == (
        "-V",
    )


def test_com_port_is_normalised() -> None:

    assert (
        normalize_com_port(
            " com7 "
        )
        == "COM7"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "7",
        "USB0",
        "COM0",
        "COM-1",
    ],
)
def test_invalid_com_port_rejected(
    value: str,
) -> None:

    with pytest.raises(
        MpCliInvalidCommand
    ):
        normalize_com_port(
            value
        )


def test_flash_with_mac_command() -> None:

    command = (
        build_flash_with_mac_command(
            com_port="COM7",
            mac=MacAddress.parse(
                "AA:BB:CC:DD:EE:01"
            ),
            profile=make_profile(),
        )
    )

    assert command == (
        "-P",
        r"C:\firmware\stock.bin",

        "-c",
        "COM7",

        "-b",
        "1000000",

        "-x",
        "AA:BB:CC:DD:EE:01",

        "-n",
        "11223344",

        "-k",
        VALID_KEY,
        "-r",
    )


def test_flash_can_omit_reboot() -> None:

    command = build_flash_with_mac_command(
        com_port="COM3",
        mac=MacAddress.parse(
            "001122334455"
        ),
        profile=make_profile(
            reboot=False
        ),
    )

    assert "-r" not in command


def test_read_mac_command() -> None:

    assert build_read_mac_command(
        com_port="com12"
    ) == (
        "-c",
        "COM12",
        "-b",
        "1000000",
        "-I",
    )


def test_reboot_command() -> None:

    assert build_reboot_command(
        com_port="COM4"
    ) == (
        "-c",
        "COM4",
        "-b",
        "1000000",
        "-r",
    )

def test_secret_key_is_redacted_from_logged_command() -> None:

    command = (
        "-P",
        "image.bin",
        "-x",
        "AABBCCDDEE01",
        "-n",
        "11223344",
        "-k",
        VALID_KEY,
        "-r",
    )

    safe = sanitise_command(
        command
    )

    assert VALID_KEY not in safe

    assert "<REDACTED>" in safe

    assert "AABBCCDDEE01" in safe

    