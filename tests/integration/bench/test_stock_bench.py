"""Stock bench runner through the real MPCLI driver against the stub."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

from stationapp.bench.mac_pool import BenchMacPool, MacRangeError
from stationapp.bench.runner import BenchSettings, MpCliBenchHardware, RfMode, StockBenchRunner
from stationapp.drivers.mpcli.process import MpCliProcessRunner
from stationapp.services.simulation import SimulatedGoldenRig, parse_failures

STUB = Path(__file__).parents[2] / "fixtures" / "mpcli" / "stub_mpcli.py"
KEY = "00112233445566778899AABBCCDDEEFF" * 2


@pytest.fixture
def bench(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_MODE", "success")
    monkeypatch.delenv("STUB_MPCLI_READBACK_MAC", raising=False)
    monkeypatch.setenv("STUB_MPCLI_STATE_DIR", str(tmp_path))
    firmware = tmp_path / "stock.bin"
    firmware.write_bytes(b"stock-firmware")

    pool = BenchMacPool(tmp_path / "bench_state.json")
    pool.set_range("02:5A:00:00:00:01", "02:5A:00:00:00:10")

    def hardware(settings):
        runner = MpCliProcessRunner(Path(sys.executable), base_arguments=(str(STUB),), termination_grace_seconds=0.2, poll_interval_seconds=0.01)
        return MpCliBenchHardware(settings, runner=runner)

    def make_runner(rf=None):
        return StockBenchRunner(hardware_factory=hardware, mac_pool=pool, results_dir=tmp_path / "results", rf_tester=rf)

    settings = BenchSettings(mpcli_path=Path(sys.executable), firmware=firmware, settle_s=0)
    return {"make": make_runner, "settings": settings, "pool": pool, "tmp": tmp_path}


@pytest.mark.integration
def test_three_slots_one_failing_port(bench, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_FAIL_PORTS", "COM12")
    updates = []
    run = bench["make"]().run(bench["settings"], {1: "com11", 2: "COM12", 3: "COM13"}, RfMode.SKIP, on_update=updates.append)

    assert run.slots[1].verdict == "PASS (no RF)"
    assert run.slots[1].readback == "MATCH" and run.slots[1].readback_mac == run.slots[1].mac
    assert run.slots[2].verdict == "FAIL" and run.slots[2].flash == "FAILED"
    assert run.slots[2].message.startswith("MPCLI ERR 3 (Open file error)")
    assert run.slots[3].verdict == "PASS (no RF)"
    assert {u.stage for u in updates} >= {"Flashing firmware", "Writing MAC", "Reading back MAC", "Done"}

    rows = list(csv.DictReader(run.csv_path.open(encoding="utf-8")))
    assert [row["verdict"] for row in rows] == ["PASS (no RF)", "FAIL", "PASS (no RF)"]
    log = (run.log_dir / "slot1_COM11.log").read_text(encoding="utf-8")
    assert KEY not in log
    assert "MAC written" in log


@pytest.mark.integration
def test_readback_mismatch_fails_slot(bench, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_READBACK_MAC", "AABBCCDDEEFF")
    run = bench["make"]().run(bench["settings"], {1: "COM11"}, RfMode.SKIP)
    assert run.slots[1].readback == "MISMATCH"
    assert run.slots[1].verdict == "FAIL"


@pytest.mark.integration
def test_golden_rig_rf_only_for_verified_slots(bench, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_FAIL_PORTS", "COM12")
    rig = SimulatedGoldenRig(delay_seconds=0, failures=parse_failures("rf:3"))
    runner = bench["make"](rig)
    run = runner.run(bench["settings"], {1: "COM11", 2: "COM12", 3: "COM13"}, RfMode.GOLDEN_RIG)

    # Programming done; RF waits for the operator to reset the modules.
    assert [slot.slot_number for slot in run.awaiting_rig_rf] == [1, 3]
    assert run.csv_path is None and run.slots[1].verdict == "RF PENDING"

    run = runner.run_golden_rig_rf(run)
    assert run.slots[1].rf == "PASS" and run.slots[1].verdict == "PASS"
    assert run.slots[2].rf == "NOT TESTED"
    assert run.slots[3].rf == "FAIL" and "CONNECT_FAILED" in run.slots[3].rf_detail


@pytest.mark.integration
def test_manual_rf_waits_for_operator(bench):
    runner = bench["make"]()
    run = runner.run(bench["settings"], {1: "COM11", 2: "COM13"}, RfMode.MANUAL)

    assert [slot.slot_number for slot in run.awaiting_manual_rf] == [1, 2]
    assert run.csv_path is None

    run = runner.complete_manual_rf(run, {1: True, 2: False})
    assert run.slots[1].verdict == "PASS"
    assert run.slots[2].verdict == "RF FAIL"
    assert run.csv_path.exists()


@pytest.mark.integration
def test_macs_are_never_reused_even_after_failure(bench, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_FAIL_PORTS", "COM11")
    first = bench["make"]().run(bench["settings"], {1: "COM11"}, RfMode.SKIP)
    second = bench["make"]().run(bench["settings"], {1: "COM13"}, RfMode.SKIP)

    assert first.slots[1].verdict == "FAIL"
    assert first.slots[1].mac == "02:5A:00:00:00:01"
    assert second.slots[1].mac == "02:5A:00:00:00:02"

    reopened = BenchMacPool(bench["tmp"] / "bench_state.json")
    assert str(reopened.peek_next()) == "02:5A:00:00:00:03"
    assert reopened.remaining() == 14


@pytest.mark.integration
def test_rejects_bad_input(bench):
    runner = bench["make"]()
    with pytest.raises(ValueError, match="own COM port"):
        runner.run(bench["settings"], {1: "COM11", 2: "com11"}, RfMode.SKIP)
    with pytest.raises(ValueError, match="Golden Rig"):
        runner.run(bench["settings"], {1: "COM11"}, RfMode.GOLDEN_RIG)
    with pytest.raises(MacRangeError, match="multicast"):
        bench["pool"].set_range("01:00:00:00:00:00", "01:00:00:00:00:05")
    with pytest.raises(MacRangeError, match="exhausted"):
        bench["pool"].allocate(100)


@pytest.mark.integration
def test_unreachable_rig_keeps_modules_waiting_for_retry(bench):
    class DownRig:
        def run_test(self, **kwargs):
            raise ConnectionError("rig unreachable")

    runner = bench["make"](DownRig())
    run = runner.run(bench["settings"], {1: "COM11"}, RfMode.GOLDEN_RIG)
    run = runner.run_golden_rig_rf(run)

    assert "rig unreachable" in run.rf_error
    assert run.awaiting_rig_rf and run.csv_path is None      # can retry

    run = runner.finish_without_rf(run)
    assert run.slots[1].rf == "NOT TESTED" and run.slots[1].verdict == "PASS (no RF)"
    assert run.csv_path.exists()


@pytest.mark.integration
def test_qr_scan_after_run_and_history(bench):
    from stationapp.bench.runner import DuplicateQrError

    runner = bench["make"]()
    run = runner.run(bench["settings"], {1: "COM11", 2: "COM13"}, RfMode.SKIP, scan_qr=True)
    assert run.awaiting_qr and run.csv_path is None
    assert run.next_qr_slot.slot_number == 1

    runner.record_qr(run, 1, "aa11bb22")
    with pytest.raises(DuplicateQrError):
        runner.record_qr(run, 2, "AA11BB22")
    run = runner.complete_qr(run)                    # slot 2 never scanned
    assert run.slots[1].qr == "AA11BB22"
    assert run.slots[2].qr == "NOT SCANNED"
    assert run.csv_path.exists()

    # A later run may scan the same module again: allowed, but noted.
    second = bench["make"]().run(bench["settings"], {1: "COM11"}, RfMode.SKIP, scan_qr=True)
    slot = bench["make"]().record_qr(second, 1, "AA11BB22")
    assert slot.qr_note.startswith("seen before: run ")


@pytest.mark.integration
def test_old_results_file_is_kept_aside(bench, tmp_path):
    runner = bench["make"]()
    results = runner._results_dir
    results.mkdir(parents=True, exist_ok=True)
    (results / "stock_bench_results.csv").write_text("run_id,finished_at,slot\nold,x,1\n", encoding="utf-8")

    run = runner.run(bench["settings"], {1: "COM11"}, RfMode.SKIP)

    assert run.csv_path.read_text(encoding="utf-8").startswith("run_id,finished_at,mode,slot,com_port")
    assert list(results.glob("stock_bench_results_before_*.csv"))


@pytest.mark.integration
def test_failed_modules_qr_is_scanned_and_recorded(bench, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_FAIL_PORTS", "COM12")
    runner = bench["make"]()
    run = runner.run(bench["settings"], {1: "COM11", 2: "COM12"}, RfMode.SKIP, scan_qr=True)

    assert run.slots[2].verdict == "FAIL"
    assert [slot.slot_number for slot in run.qr_slots] == [1, 2]   # failed one included
    runner.record_qr(run, 1, "AA11BB22")
    runner.record_qr(run, 2, "CC33DD44")
    run = runner.complete_qr(run)

    rows = list(csv.DictReader(run.csv_path.open(encoding="utf-8")))
    assert [(row["slot"], row["qr"], row["verdict"]) for row in rows] == [
        ("1", "AA11BB22", "PASS (no RF)"),
        ("2", "CC33DD44", "FAIL"),
    ]


# ---------------------------------------------------------------------------
# Stock + Pricol: every manual step waits for the operator
# ---------------------------------------------------------------------------


class FakeFunctional:
    """PRICOLAPP / PRICOLDFU per COM port: True, False or None (no answer)."""

    def __init__(self, app=None, dfu=None):
        self.app, self.dfu = app or {}, dfu or {}
        self.calls = []

    def run_app_test(self, *, slot_number, com_port, cancellation_token=None):
        self.calls.append(("APP", com_port))
        return self.app.get(com_port, True)

    def run_dfu_test(self, *, slot_number, com_port, cancellation_token=None):
        self.calls.append(("DFU", com_port))
        return self.dfu.get(com_port, True)


def pricol_runner(bench, functional):
    from stationapp.bench.runner import MpCliBenchHardware

    pricol_pool = BenchMacPool(bench["tmp"] / "pricol_state.json")
    pricol_pool.set_range("02:5B:00:00:00:01", "02:5B:00:00:00:10")
    stock = bench["make"]()
    runner = StockBenchRunner(
        hardware_factory=stock._hardware_factory,
        mac_pool=bench["pool"],
        results_dir=bench["tmp"] / "results",
        pricol_mac_pool=pricol_pool,
        functional_tester=functional,
    )
    firmware = bench["tmp"] / "pricol.bin"
    firmware.write_bytes(b"pricol-firmware")
    pricol_settings = BenchSettings(mpcli_path=bench["settings"].mpcli_path, firmware=firmware, settle_s=0)
    return runner, pricol_settings, pricol_pool


@pytest.mark.integration
def test_stock_and_pricol_flow_waits_for_operator_at_each_manual_step(bench, monkeypatch):
    from stationapp.bench.runner import PricolStep

    functional = FakeFunctional(app={"COM13": False}, dfu={"COM14": None})
    runner, pricol_settings, pricol_pool = pricol_runner(bench, functional)
    monkeypatch.setenv("STUB_MPCLI_FAIL_PORTS", "COM12")              # slot 2 fails Stock

    run = runner.run(bench["settings"], {1: "COM11", 2: "COM12", 3: "COM13", 4: "COM14"}, RfMode.SKIP,
                     pricol_settings=pricol_settings, scan_qr=True)

    # 1. Stock done; waiting for: boot mode + reset -> PROGRAM PRICOL FIRMWARE
    assert run.pricol_step is PricolStep.PROGRAM
    assert [slot.slot_number for slot in run.pricol_candidates] == [1, 3, 4]
    assert run.slots[1].pricol_flash == "-" and run.slots[1].verdict == "PRICOL PENDING"
    assert run.slots[2].verdict == "FAIL"
    assert not run.awaiting_qr and run.csv_path is None

    # 2. PROGRAM PRICOL FIRMWARE -> waiting for: app mode + power cycle
    monkeypatch.delenv("STUB_MPCLI_FAIL_PORTS")
    run = runner.program_pricol(run)
    assert run.pricol_step is PricolStep.APP
    for number in (1, 3, 4):
        slot = run.slots[number]
        assert slot.pricol_readback == "MATCH" and slot.pricol_readback_mac == slot.pricol_mac
        assert slot.pricol_mac.startswith("02:5B") and slot.pricol_mac != slot.mac
    assert run.slots[2].pricol_mac == ""
    assert functional.calls == []
    assert pricol_pool.remaining() == 13

    # 3. RUN APP TEST -> waiting for: reset -> RUN DFU TEST
    run = runner.run_app_test(run)
    assert run.pricol_step is PricolStep.DFU
    assert [c for c in functional.calls] == [("APP", "COM11"), ("APP", "COM13"), ("APP", "COM14")]
    assert run.slots[3].app == "FAIL" and run.slots[3].verdict == "FUNCTIONAL FAIL"

    # 4. RUN DFU TEST (APP-failed modules are DFU-tested too, as in production)
    run = runner.run_dfu_test(run)
    assert run.pricol_step is PricolStep.DONE
    assert ("DFU", "COM13") in functional.calls
    assert run.awaiting_qr                                            # 5. QR scan, every module

    for number, qr in ((1, "AA000001"), (2, "AA000002"), (3, "AA000003"), (4, "AA000004")):
        runner.record_qr(run, number, qr)
    run = runner.complete_qr(run)

    rows = {row["slot"]: row for row in csv.DictReader(run.csv_path.open(encoding="utf-8"))}
    assert rows["1"]["verdict"] == "PASS (no RF)" and rows["1"]["mode"] == "STOCK_AND_PRICOL"
    assert rows["1"]["app"] == "PASS" and rows["1"]["dfu"] == "PASS"
    assert rows["2"]["verdict"] == "FAIL" and rows["2"]["qr"] == "AA000002"
    assert rows["3"]["verdict"] == "FUNCTIONAL FAIL"
    assert rows["4"]["dfu"] == "ERROR" and rows["4"]["verdict"] == "FUNCTIONAL ERROR"
    assert rows["1"]["pricol_firmware"] == "pricol.bin"


@pytest.mark.integration
def test_operator_can_stop_before_pricol_steps(bench):
    from stationapp.bench.runner import PricolStep

    runner, pricol_settings, _pool = pricol_runner(bench, FakeFunctional())
    run = runner.run(bench["settings"], {1: "COM11"}, RfMode.SKIP, pricol_settings=pricol_settings)
    run = runner.program_pricol(run)
    run = runner.finish_without_pricol(run)                           # before the APP test

    assert run.pricol_step is PricolStep.DONE
    assert run.slots[1].app == "NOT TESTED" and run.slots[1].verdict == "NOT TESTED"
    assert run.csv_path.exists()


@pytest.mark.integration
def test_pricol_needs_a_separate_mac_range(bench):
    runner, pricol_settings, pricol_pool = pricol_runner(bench, FakeFunctional())
    pricol_pool.set_range("02:5A:00:00:00:08", "02:5A:00:00:00:20")    # overlaps the Stock range
    with pytest.raises(ValueError, match="overlap"):
        runner.run(bench["settings"], {1: "COM11"}, RfMode.SKIP, pricol_settings=pricol_settings)


# ---------------------------------------------------------------------------
# Chip erase before every firmware flash
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_chip_erase_runs_before_stock_and_pricol_flash(bench, monkeypatch):
    log = bench["tmp"] / "mpcli_commands.log"
    monkeypatch.setenv("STUB_MPCLI_LOG", str(log))
    runner, pricol_settings, _pool = pricol_runner(bench, FakeFunctional())

    run = runner.run(bench["settings"], {1: "COM11"}, RfMode.SKIP, pricol_settings=pricol_settings)
    run = runner.program_pricol(run)

    steps = []
    for line in log.read_text(encoding="utf-8").splitlines():
        args = line.split()
        steps.append("ERASE" if "-E" in args else "FLASH" if "-P" in args else "MAC" if "-x" in args else "READ" if "-I" in args else line)
    assert steps == ["ERASE", "FLASH", "MAC", "READ", "ERASE", "FLASH", "MAC", "READ"]
    assert run.slots[1].readback == "MATCH" and run.slots[1].pricol_readback == "MATCH"
    assert "CHIP ERASE" in run.slots[1].log[0] and "PRICOL CHIP ERASE" in "".join(run.slots[1].log)


@pytest.mark.integration
def test_failed_chip_erase_stops_the_slot(bench, monkeypatch):
    monkeypatch.setenv("STUB_MPCLI_FAIL_ERASE_PORTS", "COM12")
    run = bench["make"]().run(bench["settings"], {1: "COM11", 2: "COM12"}, RfMode.SKIP)

    assert run.slots[1].verdict == "PASS (no RF)"
    assert run.slots[2].flash == "NOT RUN" and run.slots[2].verdict == "FAIL"
    assert run.slots[2].message.startswith("Chip erase failed")
