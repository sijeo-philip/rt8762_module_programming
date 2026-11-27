"""One MPCLI session per programming stage: [-E] -P image -x MAC -I -r.

The fixture is the bench output of
    mpcli -c COM3 -E -b 1000000 -P fw/ImgPacketFile.bin -x 11:22:33:44:55:77 -I -r
(user name in the temp paths replaced).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stationapp.domain.mac import MacAddress, MacPurpose
from stationapp.drivers.mpcli.commands import build_program_session_command
from stationapp.drivers.mpcli.configuration import MpCliProgrammingProfile
from stationapp.drivers.mpcli.driver import PHASE_FLASHING, PHASE_SESSION_ERASE, MpCliDriver
from stationapp.drivers.mpcli.parsers import parse_session_readback
from stationapp.drivers.mpcli.types import MacReadbackOutcome, ProcessOutcome, ProcessResult
from stationapp.services.programming import ProgrammingService, ProgrammingTarget

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "mpcli" / "single_session_success.txt"
BENCH_MAC = MacAddress.parse("11:22:33:44:55:77")


def result(stdout: str, return_code: int = 0) -> ProcessResult:
    return ProcessResult(("mpcli.exe",), ProcessOutcome.COMPLETED, return_code, stdout, "", 9.0)


class ScriptedRunner:
    """Answers each MPCLI call with the next scripted output."""

    def __init__(self, *outputs: tuple[str, int]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments, *, timeout_seconds, cancellation_token=None):
        self.calls.append(tuple(arguments))
        stdout, code = self.outputs.pop(0)
        return result(stdout, code)


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "fw.bin"
    path.write_bytes(b"fw")
    return path


def bench_output(mac: MacAddress = BENCH_MAC) -> str:
    return FIXTURE.read_text(encoding="utf-8").replace("11:22:33:44:55:77", mac.colon)


# --- command ---------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("erase", "flag"), [("full", "-E"), ("keep_first_4k", "-C"), ("none", None)])
def test_session_command(image, erase, flag):
    profile = MpCliProgrammingProfile(image_packet=image, chip_erase=erase)
    command = build_program_session_command(com_port="com3", mac=BENCH_MAC, profile=profile)
    expected = ["-c", "COM3", "-b", "1000000"] + ([flag] if flag else []) + ["-P", str(image), "-x", BENCH_MAC.colon, "-I", "-r"]
    assert command == tuple(expected)


# --- parser ----------------------------------------------------------------

@pytest.mark.unit
def test_bench_output_reads_back_the_mac():
    readback = parse_session_readback(result(bench_output()))
    assert readback.outcome is MacReadbackOutcome.READ
    assert readback.mac == BENCH_MAC


@pytest.mark.unit
def test_requested_mac_alone_is_not_a_readback():
    """'new MAC:' is what MPCLI was asked to write, not what flash holds."""
    text = bench_output().split("CMD[15] dumpmac")[0] + "CMD[16] reboot\n-OK-\nTask executed successfully\n"
    readback = parse_session_readback(result(text))
    assert readback.outcome is MacReadbackOutcome.NOT_FOUND
    assert readback.mac is None


@pytest.mark.unit
def test_flash_mac_differing_from_request_is_reported_as_read():
    text = bench_output().replace("Original MAC: 11:22:33:44:55:77", "Original MAC: 11:22:33:44:55:78")
    readback = parse_session_readback(result(text))
    assert readback.outcome is MacReadbackOutcome.READ
    assert readback.mac == MacAddress.parse("11:22:33:44:55:78")   # the caller sees the mismatch


@pytest.mark.unit
def test_failed_run_is_not_a_readback():
    readback = parse_session_readback(result("CMD[2] loadfw\nrecv hci evt Fail!\n", return_code=6))
    assert readback.outcome is MacReadbackOutcome.PROCESS_FAILED


# --- driver ----------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("erase", "phase"), [("full", PHASE_SESSION_ERASE), ("none", PHASE_FLASHING)])
def test_driver_makes_one_call(image, erase, phase):
    runner = ScriptedRunner((bench_output(), 0))
    phases: list[str] = []
    programming, readback = MpCliDriver(runner).program_single_session(
        com_port="COM3", mac=BENCH_MAC, profile=MpCliProgrammingProfile(image_packet=image, chip_erase=erase), timeout_seconds=90, on_phase=phases.append
    )
    assert programming.succeeded
    assert readback.mac == BENCH_MAC
    assert len(runner.calls) == 1
    assert phases == [phase]


# --- service: same-session readback -----------------------------------------

def service(runner, image: Path) -> ProgrammingService:
    profile = MpCliProgrammingProfile(image_packet=image)
    return ProgrammingService(driver=MpCliDriver(runner), stock_profile=profile, pricol_profile=profile, single_session=True)


def target(mac: MacAddress = BENCH_MAC, purpose: MacPurpose = MacPurpose.STOCK_RF_TEST) -> ProgrammingTarget:
    return ProgrammingTarget(batch_id="B-1", slot_number=1, com_port="COM3", mac=mac, purpose=purpose)


@pytest.mark.unit
def test_readback_uses_the_programming_session(image):
    runner = ScriptedRunner((bench_output(), 0))
    svc = service(runner, image)

    assert svc.program_stock(target()).programming.succeeded
    verified = svc.verify_stock_mac(target())

    assert verified.readback.mac == BENCH_MAC
    assert verified.verification.outcome.value == "MATCH"
    assert len(runner.calls) == 1            # no second MPCLI process


@pytest.mark.unit
def test_session_readback_is_used_once(image):
    """A later readback (e.g. a readback retry) reads the module again."""
    readback_only = "CMD[4] dumpmac\nOriginal MAC: 11:22:33:44:55:77\n-OK-\nTask executed successfully\n"
    runner = ScriptedRunner((bench_output(), 0), (readback_only, 0))
    svc = service(runner, image)
    svc.program_stock(target())
    svc.verify_stock_mac(target())
    again = svc.verify_stock_mac(target())

    assert again.readback.mac == BENCH_MAC
    assert len(runner.calls) == 2
    assert "-P" not in runner.calls[1] and "-I" in runner.calls[1]


@pytest.mark.unit
def test_failed_programming_leaves_no_readback(image):
    readback_only = "CMD[4] dumpmac\nOriginal MAC: 11:22:33:44:55:77\n-OK-\n"
    runner = ScriptedRunner(("recv hci evt Fail!\nDetect FW Fail\n", 6), (readback_only, 0))
    svc = service(runner, image)

    assert not svc.program_stock(target()).programming.succeeded
    svc.verify_stock_mac(target())
    assert len(runner.calls) == 2            # verified by a real read, not a stale session


@pytest.mark.unit
def test_mismatch_from_session_is_detected(image):
    text = bench_output().replace("Original MAC: 11:22:33:44:55:77", "Original MAC: 11:22:33:44:55:78")
    svc = service(ScriptedRunner((text, 0)), image)
    svc.program_stock(target())
    assert svc.verify_stock_mac(target()).verification.outcome.value == "MISMATCH"


@pytest.mark.unit
def test_pricol_stage_uses_its_own_session(image):
    pricol_mac = MacAddress.parse("AA:BB:CC:00:00:01")
    runner = ScriptedRunner((bench_output(pricol_mac), 0))
    svc = service(runner, image)
    pricol = target(pricol_mac, MacPurpose.PRICOL_PRODUCTION)
    svc.program_pricol(pricol)
    assert svc.verify_pricol_mac(pricol).readback.mac == pricol_mac
    assert len(runner.calls) == 1
