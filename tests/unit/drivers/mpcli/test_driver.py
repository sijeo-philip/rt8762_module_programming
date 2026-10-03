from pathlib import Path

from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.configuration import (
    MpCliProgrammingProfile,
)
from stationapp.drivers.mpcli.driver import (
    MpCliDriver,
)
from stationapp.drivers.mpcli.types import (
    ProcessOutcome,
    ProcessResult,
)


VALID_KEY = (
    "00112233445566778899AABBCCDDEEFF"
    "00112233445566778899AABBCCDDEEFF"
)


class FakeRunner:

    def __init__(self) -> None:
        self.arguments = None
        self.timeout_seconds = None

    def run(
        self,
        arguments,
        *,
        timeout_seconds,
        cancellation_token=None,
    ):
        self.arguments = tuple(
            arguments
        )

        self.timeout_seconds = (
            timeout_seconds
        )

        return ProcessResult(
            command=(
                "mpcli.exe",
                *self.arguments,
            ),
            outcome=(
                ProcessOutcome.COMPLETED
            ),
            return_code=0,
            stdout="OK",
            stderr="",
            duration_seconds=0.1,
        )


def test_read_mac_uses_readback_command() -> None:

    runner = FakeRunner()

    driver = MpCliDriver(
        runner
    )

    result = driver.read_mac(
        com_port="COM8",
        timeout_seconds=5.0,
    )

    assert runner.arguments == (
        "-c",
        "COM8",
        "-b",
        "1000000",
        "-I",
    )

    assert result.succeeded is True


def test_flash_builds_expected_arguments(
    tmp_path: Path,
) -> None:

    image = (
        tmp_path
        / "image.bin"
    )

    image.write_bytes(
        b"stub"
    )

    profile = MpCliProgrammingProfile(
        image_packet=image,
        product_id="11223344",
        secret_key=VALID_KEY,
    )

    runner = FakeRunner()

    driver = MpCliDriver(
        runner
    )

    result = driver.flash_with_mac(
        com_port="COM3",
        mac=MacAddress.parse(
            "AABBCCDDEE01"
        ),
        profile=profile,
        timeout_seconds=60.0,
    )

    assert runner.arguments is not None

    assert "-P" in runner.arguments

    assert str(image) in runner.arguments

    assert "-x" in runner.arguments

    assert "AABBCCDDEE01" in runner.arguments

    assert result.succeeded is True

import pytest

from stationapp.drivers.mpcli.errors import (MpCliInvalidCommand)

def test_missing_image_is_rejected(tmp_path: Path) -> None:

    profile = MpCliProgrammingProfile(
        image_packet=(
            tmp_path
            / "missing.bin"
        ),
        product_id="11223344",
        secret_key=VALID_KEY,
    )

    driver = MpCliDriver(
        FakeRunner()
    )
    with pytest.raises(MpCliInvalidCommand, match="does not exist"):
        driver.flash_with_mac(
            com_port="COM3",
            mac=MacAddress.parse(
                "AABBCCDDEE01"
            ),
            profile=profile,
            timeout_seconds=30.0,
        )

def test_structured_programming_result(tmp_path: Path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"stub")
    profile = MpCliProgrammingProfile(image_packet=image, product_id="11223344", secret_key=VALID_KEY)
    runner = FakeRunner()
    driver = MpCliDriver(runner)
    result = driver.program(com_port="COM3", mac=MacAddress.parse("AABBCCDDEE01"), profile=profile, timeout_seconds=30.0)
    assert result.succeeded is True

def test_structured_readback_result() -> None:

    class ReadbackRunner:

        def run(self, arguments, *, timeout_seconds, cancellation_token=None):
            return ProcessResult(
                command=(
                    "mpcli.exe",
                    *arguments,
                ),
                outcome=ProcessOutcome.COMPLETED,
                return_code=0,
                stdout=(
                    "BT Address: "
                    "AA:BB:CC:DD:EE:01"
                ),
                stderr="",
                duration_seconds=0.1,
            )

    driver = MpCliDriver(ReadbackRunner())
    result = driver.read_mac_structured(com_port="COM3")
    assert result.readable is True
    assert result.mac == MacAddress.parse("AABBCCDDEE01")

    