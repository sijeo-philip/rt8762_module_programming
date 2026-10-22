"""Chip erase before every firmware flash."""

from __future__ import annotations

from pathlib import Path

import pytest

from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.commands import build_chip_erase_command
from stationapp.drivers.mpcli.configuration import MpCliProgrammingProfile
from stationapp.drivers.mpcli.driver import PHASE_ERASING, PHASE_FLASHING, PHASE_WRITING_MAC, MpCliDriver
from stationapp.drivers.mpcli.types import ProcessOutcome, ProcessResult


class RecordingRunner:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on

    def run(self, arguments, *, timeout_seconds, cancellation_token=None):
        self.calls.append(tuple(arguments))
        failed = self.fail_on is not None and self.fail_on in arguments
        return ProcessResult(tuple(arguments), ProcessOutcome.COMPLETED, 6 if failed else 0, "" if failed else "-OK-", "-ERR: 6-" if failed else "", 0.1)


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "fw.bin"
    path.write_bytes(b"fw")
    return path


MAC = MacAddress.parse("02:11:11:11:11:F1")


@pytest.mark.unit
def test_chip_erase_command():
    assert build_chip_erase_command(com_port="com8") == ("-c", "COM8", "-b", "1000000", "-E", "-r")
    assert build_chip_erase_command(com_port="com8", keep_first_4k=True)[4] == "-C"


@pytest.mark.unit
def test_profile_rejects_unknown_erase_mode(image):
    with pytest.raises(ValueError, match="chip_erase"):
        MpCliProgrammingProfile(image_packet=image, chip_erase="partial")


@pytest.mark.unit
@pytest.mark.parametrize("two_step", [True, False])
def test_full_chip_erase_runs_first(image, two_step):
    runner = RecordingRunner()
    phases = []
    driver = MpCliDriver(runner)
    profile = MpCliProgrammingProfile(image_packet=image)
    program = driver.program_sequenced if two_step else driver.program
    result = program(com_port="COM8", mac=MAC, profile=profile, timeout_seconds=5, on_phase=phases.append, settle_seconds=0)

    assert result.succeeded
    assert runner.calls[0] == ("-c", "COM8", "-b", "1000000", "-E", "-r")
    assert "-P" in runner.calls[1]                                  # flash only after the erase
    assert phases[:2] == [PHASE_ERASING, PHASE_FLASHING]
    if two_step:
        assert phases[2] == PHASE_WRITING_MAC and "-x" in runner.calls[2]


@pytest.mark.unit
def test_failed_erase_stops_before_flashing(image):
    runner = RecordingRunner(fail_on="-E")
    result = MpCliDriver(runner).program_sequenced(
        com_port="COM8", mac=MAC, profile=MpCliProgrammingProfile(image_packet=image), timeout_seconds=5, settle_seconds=0
    )
    assert not result.succeeded
    assert result.message.startswith("Chip erase failed")
    assert runner.calls == [("-c", "COM8", "-b", "1000000", "-E", "-r")]   # nothing flashed


@pytest.mark.unit
def test_no_erase_when_disabled(image):
    runner = RecordingRunner()
    MpCliDriver(runner).program_sequenced(
        com_port="COM8", mac=MAC, profile=MpCliProgrammingProfile(image_packet=image, chip_erase="none"), timeout_seconds=5, settle_seconds=0
    )
    assert not any("-E" in call or "-C" in call for call in runner.calls)
