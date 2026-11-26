"""Process timing log: every step of a tray, per slot and per tray."""

from __future__ import annotations

import logging

import pytest

from stationapp.domain.batch import BatchState, ProgrammingMode
from stationapp.logging_setup import setup_logging
from stationapp.services.process_timing import TIMING_LOG_FILE, ProcessTiming
from stationapp.services.production import ProductionController
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedProgrammer,
    parse_failures,
)
from tests.integration.services.test_production_controller import (  # noqa: F401 - fixture
    JIG,
    STATION,
    TEMP_BASE,
    ports,
    run_until,
    session_factory,
)
from stationapp.data.production_repository import SqlProductionStore


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def timing_lines():
    log = logging.getLogger("test.timing")
    log.propagate = False
    log.setLevel(logging.INFO)
    handler = _Lines()
    log.addHandler(handler)
    yield log, handler.lines
    log.removeHandler(handler)


def _controller(session_factory, timing: ProcessTiming, failures: str = "") -> ProductionController:
    failure_set = parse_failures(failures)
    return ProductionController(
        station_id=STATION,
        jig_id=JIG,
        slot_count=8,
        store=SqlProductionStore(session_factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=failure_set),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=failure_set),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=failure_set),
        temporary_stock_mac_base=TEMP_BASE,
        process_timing=timing,
    )


@pytest.mark.integration
def test_full_tray_logs_each_process_per_slot_and_tray_and_summary(session_factory, timing_lines):
    log, lines = timing_lines
    controller = _controller(session_factory, ProcessTiming(logger=log), failures="stock_program:2")
    slots = (1, 2, 3, 4, 5, 6, 7, 8)
    controller.start_batch(occupied=slots, ports=ports(*slots), mode=ProgrammingMode.STOCK_AND_PRICOL)
    batch_id = controller.batch.batch_id

    run_until(controller, BatchState.QR_SCANNING)
    for number in slots:
        controller.scan_qr(f"QR-{number:04d}")
    controller.advance()                                           # COMPLETE BATCH

    text = "\n".join(lines)
    assert all(f"batch={batch_id}" in line for line in lines if not line.startswith("  "))
    assert any("START PROGRAMMING" in line and "TRAY (8 slots" in line for line in lines)
    for process in ("STOCK PROGRAM", "STOCK READBACK", "RF TEST", "PRICOL PROGRAM", "PRICOL READBACK", "APP TEST", "DFU TEST"):
        assert any(process in line and "TRAY (" in line for line in lines), process
    stock = [line for line in lines if "| STOCK PROGRAM " in line and "| slot " in line]
    assert len(stock) == 8
    assert any("slot 2 " in line and "FAIL" in line for line in stock)
    # Slot 2 failed programming: no Pricol work for it.
    assert not any("| PRICOL PROGRAM " in line and "| slot 2 " in line for line in lines)
    assert any("QR SCAN" in line and "TRAY (" in line for line in lines)
    assert "BATCH SUMMARY" in text
    assert "TOTAL" in text and "START PROGRAMMING -> COMPLETE BATCH" in text
    assert "Operator handling" in text


@pytest.mark.integration
def test_retry_is_logged_with_its_attempt(session_factory, timing_lines):
    log, lines = timing_lines
    controller = _controller(session_factory, ProcessTiming(logger=log), failures="stock_program:2")
    controller.start_batch(occupied=(1, 2), ports=ports(1, 2), mode=ProgrammingMode.STOCK_ONLY)
    run_until(controller, BatchState.STOCK_READBACK_VERIFIED)

    controller.retry_failed()

    assert any("| RETRY " in line and "TRAY (1 slots: 2)" in line for line in lines)
    assert any("| STOCK PROGRAM " in line and "slot 2 (retry 1)" in line for line in lines)


def test_timing_goes_to_its_own_file_only(tmp_path):
    setup_logging(tmp_path)
    timing = ProcessTiming()
    timing.programming_started("B-1", (1, 2))
    with timing.tray_process("B-1", "STOCK PROGRAM", (1, 2)):
        pass
    timing.completed("B-1", {1: "PASS", 2: "PASS"})
    logging.getLogger("stationapp").info("ordinary message")
    for handler in logging.getLogger("stationapp.timing").handlers + logging.getLogger().handlers:
        handler.flush()

    timing_text = (tmp_path / TIMING_LOG_FILE).read_text(encoding="utf-8")
    main_text = (tmp_path / "stationapp.log").read_text(encoding="utf-8")
    assert "STOCK PROGRAM" in timing_text and "BATCH SUMMARY" in timing_text
    assert "ordinary message" not in timing_text
    assert "STOCK PROGRAM" not in main_text

    for name in ("stationapp.timing", None):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
