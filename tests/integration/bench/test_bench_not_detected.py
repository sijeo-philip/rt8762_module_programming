"""Bench: a slot where MPCLI finds no module is skipped and its MAC kept."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from stationapp.bench.mac_pool import BenchMacPool
from stationapp.bench.runner import BenchSettings, RfMode, StockBenchRunner
from stationapp.bench.simulated import SimulatedBenchHardware
from stationapp.drivers.mpcli.parsers import module_not_detected, parse_programming_result
from stationapp.drivers.mpcli.types import ProcessOutcome, ProcessResult, ProgrammingOutcome
from tests.integration.bench.test_stock_bench import FakeFunctional

# Real MPCLI output from the bench logs (COM6, no module seated).
ERR6_NO_MODULE = """CMD[1] connect COM6
Connecting COM6 at 115200 OK
-OK-
CMD[2] loadfw --autodetect "D:/mpcli/fw"
-ERR: 6-
disconnect -OK-
Task execution failure
recv hci evt Fail!
Detect FW Fail"""


@pytest.mark.unit
def test_err6_before_the_loader_means_no_module():
    result = parse_programming_result(ProcessResult(("-c", "COM6", "-E", "-r"), ProcessOutcome.COMPLETED, 6, ERR6_NO_MODULE, "", 5.7))
    assert result.outcome is ProgrammingOutcome.NOT_DETECTED and result.module_not_detected
    assert result.message.startswith("MPCLI ERR 6") and "NO MODULE DETECTED" in result.message
    # Once the loader is running, a failure is a real programming failure.
    assert not module_not_detected("Download FW success, prepare to trigger\n-ERR: 6-\nrecv hci evt Fail!")


@pytest.fixture
def pools(tmp_path: Path):
    stock = BenchMacPool(tmp_path / "stock.json")
    stock.set_range("02:5A:00:00:00:01", "02:5A:00:00:00:10")
    pricol = BenchMacPool(tmp_path / "pricol.json")
    pricol.set_range("02:5B:00:00:00:01", "02:5B:00:00:00:10")
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    return stock, pricol, firmware


def make_runner(tmp_path, stock, pricol, absent):
    return StockBenchRunner(
        hardware_factory=lambda settings: SimulatedBenchHardware(delay_s=0.0, absent_ports=frozenset(absent)),
        mac_pool=stock,
        results_dir=tmp_path / "results",
        pricol_mac_pool=pricol,
        functional_tester=FakeFunctional(),
    )


@pytest.mark.integration
def test_absent_module_is_skipped_and_its_mac_returned(tmp_path, pools):
    stock, pricol, firmware = pools
    runner = make_runner(tmp_path, stock, pricol, {"COM13"})
    settings = BenchSettings(mpcli_path=tmp_path / "mpcli.exe", firmware=firmware, settle_s=0)

    before = stock.remaining()
    run = runner.run(settings, {1: "COM11", 2: "COM12", 3: "COM13"}, RfMode.SKIP,
                     scan_qr=True, pricol_settings=BenchSettings(mpcli_path=tmp_path / "m.exe", firmware=firmware, settle_s=0))

    absent = run.slots[3]
    assert absent.not_detected and absent.mac == "" and absent.verdict == "NOT DETECTED"
    assert absent.rf == "-"                                         # not even "NOT TESTED"
    assert stock.remaining() == before - 2                          # only 2 MACs used
    assert [slot.slot_number for slot in run.pricol_candidates] == [1, 2]

    run = runner.program_pricol(run)
    run = runner.run_app_test(run)
    run = runner.run_dfu_test(run)
    assert [slot.slot_number for slot in run.qr_slots] == [1, 2]    # no QR for slot 3
    assert pricol.remaining() == 16 - 2
    runner.record_qr(run, 1, "AA000001")
    runner.record_qr(run, 2, "AA000002")
    run = runner.complete_qr(run)

    rows = {row["slot"]: row for row in csv.DictReader(run.csv_path.open(encoding="utf-8"))}
    assert rows["3"]["verdict"] == "NOT DETECTED" and rows["3"]["mac"] == "" and rows["3"]["qr"] == ""
    assert rows["1"]["verdict"] == "PASS (no RF)"

    # The released MAC is used by the next run.
    nxt = runner.run(settings, {1: "COM11"}, RfMode.SKIP)
    assert nxt.slots[1].mac == "02:5A:00:00:00:03"


@pytest.mark.integration
def test_no_module_detected_without_chip_erase(tmp_path, pools):
    stock, pricol, firmware = pools
    runner = make_runner(tmp_path, stock, pricol, {"COM12"})
    settings = BenchSettings(mpcli_path=tmp_path / "mpcli.exe", firmware=firmware, settle_s=0, chip_erase="none")
    run = runner.run(settings, {1: "COM11", 2: "COM12"}, RfMode.SKIP)
    assert run.slots[2].verdict == "NOT DETECTED"
    assert stock.remaining() == 15


@pytest.mark.integration
def test_module_missing_at_pricol_stage_releases_only_the_pricol_mac(tmp_path, pools):
    stock, pricol, firmware = pools
    absent: set[str] = set()
    runner = StockBenchRunner(
        hardware_factory=lambda settings: SimulatedBenchHardware(delay_s=0.0, absent_ports=frozenset(absent)),
        mac_pool=stock, results_dir=tmp_path / "results", pricol_mac_pool=pricol, functional_tester=FakeFunctional(),
    )
    settings = BenchSettings(mpcli_path=tmp_path / "mpcli.exe", firmware=firmware, settle_s=0)
    run = runner.run(settings, {1: "COM11"}, RfMode.SKIP, scan_qr=True, pricol_settings=settings)
    absent.add("COM11")                                             # unseated before PROGRAM PRICOL FIRMWARE
    run = runner.program_pricol(run)

    slot = run.slots[1]
    assert slot.pricol_flash == "NOT DETECTED" and slot.pricol_mac == ""
    assert slot.verdict == "PRICOL FAIL"
    assert pricol.remaining() == 16                                 # Pricol MAC given back
    assert [item.slot_number for item in run.qr_slots] == [1]      # Stock-programmed module: QR still scanned
