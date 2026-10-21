"""Bench run of the production flow without the LAN server.

Stock only:      Stock flash -> MAC write -> readback -> RF -> QR
Stock + Pricol:  ... RF -> [operator: boot mode + reset] Pricol flash ->
                 Pricol MAC write -> readback -> [operator: application
                 mode + power cycle] PRICOLAPP -> [operator: reset]
                 PRICOLDFU -> QR

Every step that needs the modules switched or reset by hand waits for the
operator (run_golden_rig_rf, program_pricol, run_app_test, run_dfu_test).

Qt-free so it can be tested and scripted. One worker per selected slot,
as in production. Results go to a CSV (one row per slot per run) and to
per-slot MPCLI logs.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from stationapp.bench.mac_pool import BenchMacPool
from stationapp.concurrency.cancellation import CancellationToken
from stationapp.domain.golden_rig import GoldenRigOutcome, GoldenRigTarget
from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.commands import sanitise_command
from stationapp.drivers.mpcli.configuration import MpCliProgrammingProfile
from stationapp.drivers.mpcli.driver import MpCliDriver
from stationapp.drivers.mpcli.parsers import compare_readback, parse_programming_result
from stationapp.drivers.mpcli.process import MpCliProcessRunner
from stationapp.drivers.mpcli.types import (
    IdentityOutcome,
    MacReadbackResult,
    ProcessResult,
    ProgrammingResult,
)

logger = logging.getLogger(__name__)


class RfMode(str, Enum):
    SKIP = "SKIP"
    GOLDEN_RIG = "GOLDEN_RIG"
    MANUAL = "MANUAL"


class PricolStep(str, Enum):
    """Where a Stock + Pricol run is in its Pricol part."""

    NOT_REACHED = "NOT_REACHED"   # Stock / RF still running
    PROGRAM = "PROGRAM"           # waiting: boot mode + reset, PROGRAM PRICOL FIRMWARE
    APP = "APP"                   # waiting: application mode + power cycle, RUN APP TEST
    DFU = "DFU"                   # waiting: reset, RUN DFU TEST
    DONE = "DONE"


PRICOL_WAITING = (PricolStep.PROGRAM, PricolStep.APP, PricolStep.DFU)


@dataclass(frozen=True, slots=True)
class BenchSettings:
    mpcli_path: Path
    firmware: Path
    baud: int = 1_000_000
    two_step: bool = True
    flash_timeout_s: float = 90.0
    mac_timeout_s: float = 15.0
    readback_timeout_s: float = 10.0
    settle_s: float = 2.0


@dataclass(slots=True)
class SlotRun:
    slot_number: int
    com_port: str
    mac: str = ""
    stage: str = "Waiting"
    progress: int = 0
    flash: str = "-"
    mac_write: str = "-"
    readback_mac: str = ""
    readback: str = "-"
    rf: str = "-"
    rf_detail: str = ""
    verdict: str = ""
    message: str = ""
    seconds: float = 0.0
    qr: str = ""                     # scanned module QR, or "NOT SCANNED"
    qr_note: str = ""                # e.g. seen in an earlier bench run
    # Stock + Pricol only
    pricol_mac: str = ""
    pricol_flash: str = "-"
    pricol_mac_write: str = "-"
    pricol_readback_mac: str = ""
    pricol_readback: str = "-"
    app: str = "-"                   # PASS / FAIL / ERROR / NOT TESTED
    app_detail: str = ""
    dfu: str = "-"
    dfu_detail: str = ""
    aborted: bool = False            # workflow restarted before this module finished
    log: list[str] = field(default_factory=list)

    @property
    def readback_ok(self) -> bool:
        return self.readback == "MATCH"

    @property
    def pricol_ok(self) -> bool:
        return self.pricol_readback == "MATCH"


@dataclass(slots=True)
class BenchRun:
    run_id: str
    rf_mode: RfMode
    firmware: Path
    firmware_sha256: str
    slots: dict[int, SlotRun]
    started_at: datetime
    finished_at: datetime | None = None
    csv_path: Path | None = None
    log_dir: Path | None = None
    cancelled: bool = False
    rf_error: str | None = None      # last Golden Rig failure; RF can be retried
    scan_qr: bool = False            # operator scans module QRs after RF
    qr_done: bool = False
    pricol_settings: BenchSettings | None = None   # set: Stock + Pricol run
    pricol_firmware_sha256: str = ""
    pricol_step: PricolStep = PricolStep.NOT_REACHED

    @property
    def pricol(self) -> bool:
        return self.pricol_settings is not None

    @property
    def pricol_pending(self) -> bool:
        return self.pricol and self.pricol_step in PRICOL_WAITING

    @property
    def pricol_candidates(self) -> list[SlotRun]:
        """Modules that passed Stock programming, readback and RF (or RF
        was skipped) - the ones that get Pricol firmware."""
        return [
            slot for _, slot in sorted(self.slots.items())
            if slot.readback_ok and slot.rf not in {"FAIL", "ERROR", "AWAITING OPERATOR", "AWAITING RESET"}
        ]

    @property
    def app_candidates(self) -> list[SlotRun]:
        return [slot for _, slot in sorted(self.slots.items()) if slot.pricol_ok and slot.app == "-"]

    @property
    def dfu_candidates(self) -> list[SlotRun]:
        """As in production, DFU follows any APP verdict (PASS or FAIL)."""
        return [slot for _, slot in sorted(self.slots.items()) if slot.app in {"PASS", "FAIL"} and slot.dfu == "-"]

    @property
    def awaiting_manual_rf(self) -> list[SlotRun]:
        return [slot for slot in self.slots.values() if slot.rf == "AWAITING OPERATOR"]

    @property
    def awaiting_rig_rf(self) -> list[SlotRun]:
        """Programmed slots waiting for the operator to reset them and press RUN RF TEST."""
        return [slot for slot in self.slots.values() if slot.rf == "AWAITING RESET"]

    @property
    def rf_pending(self) -> bool:
        return bool(self.awaiting_manual_rf or self.awaiting_rig_rf)

    @property
    def qr_slots(self) -> list[SlotRun]:
        """Every module in the run, in slot order: each QR is scanned and
        recorded whatever the module's test result."""
        return [slot for _, slot in sorted(self.slots.items())]

    @property
    def awaiting_qr(self) -> bool:
        return self.scan_qr and not self.qr_done and not self.rf_pending and not self.pricol_pending and bool(self.qr_slots)

    @property
    def next_qr_slot(self) -> SlotRun | None:
        return next((slot for slot in self.qr_slots if not slot.qr), None)


class DuplicateQrError(ValueError):
    """The QR is already used by another module in this run."""


NOT_SCANNED = "NOT SCANNED"
CSV_HEADER = [
    "run_id", "finished_at", "mode", "slot", "com_port", "mac", "flash", "mac_write",
    "readback_mac", "readback", "rf_mode", "rf", "rf_detail",
    "pricol_mac", "pricol_flash", "pricol_mac_write", "pricol_readback_mac", "pricol_readback",
    "app", "app_detail", "dfu", "dfu_detail",
    "qr", "qr_note", "verdict", "seconds", "message",
    "firmware", "firmware_sha256", "pricol_firmware", "pricol_firmware_sha256",
]


class BenchHardware(Protocol):
    def flash(self, com_port: str, token: CancellationToken) -> ProgrammingResult: ...
    def write_mac(self, com_port: str, mac: MacAddress, token: CancellationToken) -> ProgrammingResult: ...
    def flash_with_mac(self, com_port: str, mac: MacAddress, token: CancellationToken) -> ProgrammingResult: ...
    def read_mac(self, com_port: str, token: CancellationToken) -> MacReadbackResult: ...


class MpCliBenchHardware:
    """Real MPCLI, one subprocess per call."""

    def __init__(self, settings: BenchSettings, *, runner: MpCliProcessRunner | None = None) -> None:
        self._settings = settings
        self._driver = MpCliDriver(runner or MpCliProcessRunner(Path(settings.mpcli_path)))
        self._profile = MpCliProgrammingProfile(
            image_packet=Path(settings.firmware),
            baud=settings.baud,
        )

    def flash(self, com_port, token):
        raw = self._driver.flash(com_port=com_port, profile=self._profile, timeout_seconds=self._settings.flash_timeout_s, cancellation_token=token)
        return parse_programming_result(raw)

    def write_mac(self, com_port, mac, token):
        raw = self._driver.set_mac(com_port=com_port, mac=mac, profile=self._profile, timeout_seconds=self._settings.mac_timeout_s, cancellation_token=token)
        return parse_programming_result(raw)

    def flash_with_mac(self, com_port, mac, token):
        return self._driver.program(com_port=com_port, mac=mac, profile=self._profile, timeout_seconds=self._settings.flash_timeout_s, cancellation_token=token)

    def read_mac(self, com_port, token):
        return self._driver.read_mac_structured(com_port=com_port, baud=self._settings.baud, timeout_seconds=self._settings.readback_timeout_s, cancellation_token=token)


HardwareFactory = Callable[[BenchSettings], BenchHardware]
Listener = Callable[[SlotRun], None]


class StockBenchRunner:
    def __init__(self, *, hardware_factory: HardwareFactory, mac_pool: BenchMacPool, results_dir: Path, rf_tester=None, station_id: str = "BENCH", jig_id: str = "BENCH", pricol_mac_pool: BenchMacPool | None = None, functional_tester=None) -> None:
        self._hardware_factory = hardware_factory
        self._mac_pool = mac_pool
        self._pricol_mac_pool = pricol_mac_pool
        self._functional_tester = functional_tester
        self._results_dir = Path(results_dir)
        self._rf_tester = rf_tester
        self._qr_history: dict[str, str] | None = None   # QR -> "run R slot S"
        self._station_id = station_id
        self._jig_id = jig_id
        self._lock = threading.Lock()

    @property
    def golden_rig_available(self) -> bool:
        return self._rf_tester is not None

    @property
    def pricol_available(self) -> bool:
        return self._pricol_mac_pool is not None and self._functional_tester is not None

    # ------------------------------------------------------------------

    def run(self, settings: BenchSettings, slots: dict[int, str], rf_mode: RfMode, *, on_update: Listener | None = None, token: CancellationToken | None = None, scan_qr: bool = False, pricol_settings: BenchSettings | None = None) -> BenchRun:
        if not slots:
            raise ValueError("Select at least one slot")
        ports = [port.strip().upper() for port in slots.values()]
        if len(set(ports)) != len(ports):
            raise ValueError("Each selected slot needs its own COM port")
        if not Path(settings.firmware).is_file():
            raise ValueError(f"Firmware image not found: {settings.firmware}")
        if rf_mode is RfMode.GOLDEN_RIG and self._rf_tester is None:
            raise ValueError("No approved Golden Rig is available for RF testing")
        if pricol_settings is not None:
            self._check_pricol_ready(pricol_settings, len(slots))

        token = token or CancellationToken()
        hardware = self._hardware_factory(settings)
        notify = on_update or (lambda _slot: None)

        started = datetime.now()
        run_id = started.strftime("%Y%m%d-%H%M%S")
        macs = self._mac_pool.allocate(len(slots))
        run = BenchRun(
            run_id=run_id,
            rf_mode=rf_mode,
            firmware=Path(settings.firmware),
            firmware_sha256=_sha256(Path(settings.firmware)),
            slots={
                number: SlotRun(number, slots[number].strip().upper(), mac=str(mac))
                for number, mac in zip(sorted(slots), macs, strict=True)
            },
            started_at=started,
            scan_qr=scan_qr,
            pricol_settings=pricol_settings,
            pricol_firmware_sha256=_sha256(Path(pricol_settings.firmware)) if pricol_settings else "",
        )
        for slot in run.slots.values():
            notify(replace(slot, log=[]))

        with ThreadPoolExecutor(max_workers=len(run.slots), thread_name_prefix="bench-slot") as pool:
            list(pool.map(lambda slot: self._program_slot(hardware, settings, slot, token, notify), run.slots.values()))

        run.cancelled = token.is_cancelled
        if not run.cancelled:
            self._rf(run, token, notify)
        return self._finish(run, notify)

    def complete_manual_rf(self, run: BenchRun, decisions: dict[int, bool], *, on_update: Listener | None = None) -> BenchRun:
        """Record the operator's RF observations (MANUAL mode)."""
        notify = on_update or (lambda _slot: None)
        for slot in run.awaiting_manual_rf:
            passed = decisions.get(slot.slot_number)
            if passed is None:
                slot.rf, slot.rf_detail = "NOT TESTED", "operator gave no result"
            else:
                slot.rf = "PASS" if passed else "FAIL"
                slot.rf_detail = "operator confirmed connect/disconnect" if passed else "operator reported failure"
        return self._finish(run, notify)

    def run_golden_rig_rf(self, run: BenchRun, *, on_update: Listener | None = None, token: CancellationToken | None = None) -> BenchRun:
        """Golden Rig RF test, started by the operator once the modules are
        reset from boot (download) mode into application mode.

        If the rig cannot be used (unreachable, busy, ...) the slots stay
        waiting and run.rf_error says why, so the operator can retry.
        """
        notify = on_update or (lambda _slot: None)
        token = token or CancellationToken()
        waiting = run.awaiting_rig_rf
        if not waiting:
            raise ValueError("No module is waiting for the RF test")
        if self._rf_tester is None:
            raise ValueError("No approved Golden Rig is available for RF testing")

        run.rf_error = None
        for slot in waiting:
            slot.stage, slot.progress = "Golden Rig RF test", 95
            notify(replace(slot, log=[]))
        try:
            report = self._rf_tester.run_test(
                batch_id=f"BENCH-{run.run_id}",
                station_id=self._station_id,
                jig_id=self._jig_id,
                targets=tuple(GoldenRigTarget(slot.slot_number, MacAddress.parse(slot.mac)) for slot in waiting),
                cancel_event=token.event,
            )
        except Exception as exc:  # noqa: BLE001 - reported; the operator may retry
            logger.exception("Golden Rig RF test failed")
            run.rf_error = f"Golden Rig: {exc}"
            for slot in waiting:
                slot.stage, slot.progress = "RF not run - retry or finish without RF", 90
                slot.rf_detail = run.rf_error
                notify(replace(slot, log=[]))
            return run

        for result in report.response.results:
            slot = run.slots[result.slot_number]
            slot.rf = result.outcome.value
            parts = [f"connected={result.connected}", f"disconnected={result.disconnected}"]
            if result.reported_mac:
                parts.append(f"reported={result.reported_mac}")
            if result.error_code:
                parts.append(result.error_code)
            if result.detail:
                parts.append(result.detail)
            slot.rf_detail = ", ".join(parts)
        for slot in run.awaiting_rig_rf:          # rig returned no result for it
            slot.rf, slot.rf_detail = "ERROR", "no result from the Golden Rig"
        return self._finish(run, notify)

    def finish_without_rf(self, run: BenchRun, *, on_update: Listener | None = None) -> BenchRun:
        """Operator ends a run that is waiting for RF without testing."""
        notify = on_update or (lambda _slot: None)
        for slot in run.awaiting_rig_rf + run.awaiting_manual_rf:
            slot.rf, slot.rf_detail = "NOT TESTED", "operator finished without RF"
        run.rf_error = None
        return self._finish(run, notify)

    # ------------------------------------------------------- Pricol part

    def _check_pricol_ready(self, pricol_settings: BenchSettings, count: int) -> None:
        if not self.pricol_available:
            raise ValueError("Stock + Pricol is not available: no Pricol MAC range or functional tester")
        if not Path(pricol_settings.firmware).is_file():
            raise ValueError(f"Pricol firmware image not found: {pricol_settings.firmware}")
        pool = self._pricol_mac_pool
        if pool.start is None:
            raise ValueError("Set the Pricol test MAC range first")
        if pool.remaining() < count:
            raise ValueError(f"Only {pool.remaining()} Pricol test MAC(s) left for {count} module(s)")
        stock = self._mac_pool
        if stock.start is not None and _overlap(stock.start, stock.end, pool.start, pool.end):
            raise ValueError("The Stock and Pricol test MAC ranges overlap; use separate ranges")

    def program_pricol(self, run: BenchRun, *, on_update: Listener | None = None, token: CancellationToken | None = None) -> BenchRun:
        """PROGRAM PRICOL FIRMWARE: the operator has switched the modules
        back to boot (download) mode and reset them."""
        notify = on_update or (lambda _slot: None)
        token = token or CancellationToken()
        if run.pricol_step is not PricolStep.PROGRAM:
            raise ValueError("No module is waiting for Pricol programming")
        settings = run.pricol_settings
        targets = [slot for slot in run.pricol_candidates if slot.pricol_flash == "-"]
        unassigned = [slot for slot in targets if not slot.pricol_mac]
        if unassigned:
            macs = self._pricol_mac_pool.allocate(len(unassigned))
            for slot, mac in zip(unassigned, macs, strict=True):
                slot.pricol_mac = str(mac)
        hardware = self._hardware_factory(settings)

        def program(slot: SlotRun) -> None:
            started = time.monotonic()

            def step(stage: str, progress: int) -> None:
                slot.stage, slot.progress = f"Pricol: {stage}", progress
                notify(replace(slot, log=[]))

            outcome = self._flash_write_verify(hardware, settings, slot, MacAddress.parse(slot.pricol_mac), token, step, "PRICOL ")
            slot.pricol_flash, slot.pricol_mac_write = outcome.flash, outcome.mac_write
            slot.pricol_readback_mac, slot.pricol_readback = outcome.readback_mac, outcome.readback
            if outcome.message:
                slot.message = f"Pricol: {outcome.message}"
            slot.seconds = round(slot.seconds + time.monotonic() - started, 1)

        with ThreadPoolExecutor(max_workers=max(1, len(targets)), thread_name_prefix="bench-pricol") as pool:
            list(pool.map(program, targets))

        run.pricol_step = PricolStep.APP if run.app_candidates else PricolStep.DONE
        if token.is_cancelled:
            run.cancelled = True
            run.pricol_step = PricolStep.DONE
        return self._finish(run, notify)

    def run_app_test(self, run: BenchRun, *, on_update: Listener | None = None, token: CancellationToken | None = None) -> BenchRun:
        """RUN APP TEST: modules in application mode and power-cycled."""
        if run.pricol_step is not PricolStep.APP:
            raise ValueError("No module is waiting for the APP test")
        self._functional(run, dfu=False, notify=on_update or (lambda _slot: None), token=token or CancellationToken())
        run.pricol_step = PricolStep.DFU if run.dfu_candidates else PricolStep.DONE
        return self._finish(run, on_update or (lambda _slot: None))

    def run_dfu_test(self, run: BenchRun, *, on_update: Listener | None = None, token: CancellationToken | None = None) -> BenchRun:
        """RUN DFU TEST: modules reset again."""
        if run.pricol_step is not PricolStep.DFU:
            raise ValueError("No module is waiting for the DFU test")
        self._functional(run, dfu=True, notify=on_update or (lambda _slot: None), token=token or CancellationToken())
        run.pricol_step = PricolStep.DONE
        return self._finish(run, on_update or (lambda _slot: None))

    def abort_run(self, run: BenchRun, *, on_update: Listener | None = None) -> BenchRun:
        """The operator restarts the workflow: record this run as it stands.

        Modules still waiting for a step are recorded as ABORTED; finished
        results are kept. Used MACs stay used. A run already recorded is
        returned unchanged.
        """
        if run.csv_path is not None:
            return run
        for slot in run.slots.values():
            if "PENDING" in _verdict(slot, run) or not slot.verdict:
                slot.aborted = True
                slot.message = (slot.message + "; " if slot.message else "") + "workflow restarted by operator"
            if slot.rf in {"AWAITING OPERATOR", "AWAITING RESET"}:
                slot.rf, slot.rf_detail = "NOT TESTED", "workflow restarted"
            if not slot.qr:
                slot.qr = NOT_SCANNED
        run.rf_error = None
        if run.pricol:
            run.pricol_step = PricolStep.DONE
        run.qr_done = True
        return self._finish(run, on_update or (lambda _slot: None))

    def finish_without_pricol(self, run: BenchRun, *, on_update: Listener | None = None) -> BenchRun:
        """Operator stops a Stock + Pricol run before its remaining steps."""
        step = run.pricol_step
        if step is PricolStep.PROGRAM:
            for slot in run.pricol_candidates:
                if slot.pricol_flash == "-":
                    slot.pricol_flash = "NOT RUN"
        if step in (PricolStep.PROGRAM, PricolStep.APP):
            for slot in run.slots.values():
                if slot.pricol_ok and slot.app == "-":
                    slot.app = "NOT TESTED"
        if step in PRICOL_WAITING:
            for slot in run.slots.values():
                if slot.app in {"PASS", "FAIL"} and slot.dfu == "-":
                    slot.dfu = "NOT TESTED"
            run.pricol_step = PricolStep.DONE
        return self._finish(run, on_update or (lambda _slot: None))

    def _functional(self, run: BenchRun, *, dfu: bool, notify: Listener, token: CancellationToken) -> None:
        tester = self._functional_tester
        targets = run.dfu_candidates if dfu else run.app_candidates
        label = "PRICOLDFU" if dfu else "PRICOLAPP"

        def test(slot: SlotRun) -> None:
            slot.stage, slot.progress = f"{label} test", 97
            notify(replace(slot, log=[]))
            try:
                call = tester.run_dfu_test if dfu else tester.run_app_test
                outcome = call(slot_number=slot.slot_number, com_port=slot.com_port, cancellation_token=token)
                passed = getattr(outcome, "passed", outcome)
                detail = getattr(outcome, "detail", "") or ""
                response = (getattr(outcome, "response", "") or "").strip()
                if response:
                    detail = f"{detail} | response: {response[:120]}" if detail else f"response: {response[:120]}"
                    slot.log.append(f"=== {label} | {slot.com_port}\n{getattr(outcome, 'response', '')}\n")
            except Exception as exc:  # noqa: BLE001 - one slot must not stop the others
                logger.exception("Bench %s slot %s failed", label, slot.slot_number)
                passed, detail = None, f"{type(exc).__name__}: {exc}"
            result = "PASS" if passed is True else "FAIL" if passed is False else "ERROR"
            if dfu:
                slot.dfu, slot.dfu_detail = result, detail
            else:
                slot.app, slot.app_detail = result, detail
            notify(replace(slot, log=[]))

        with ThreadPoolExecutor(max_workers=max(1, len(targets)), thread_name_prefix="bench-functional") as pool:
            list(pool.map(test, targets))

    # ------------------------------------------------------------- QR scan

    def record_qr(self, run: BenchRun, slot_number: int, code: str, *, on_update: Listener | None = None) -> SlotRun:
        """Bind a scanned QR to the module in slot_number."""
        notify = on_update or (lambda _slot: None)
        code = code.strip().upper()
        slot = run.slots.get(slot_number)
        if slot is None or slot not in run.qr_slots:
            raise ValueError(f"Slot {slot_number} does not take a QR in this run")
        if slot.qr:
            raise ValueError(f"Slot {slot_number} already has QR {slot.qr}")
        owner = next((other.slot_number for other in run.slots.values() if other.qr == code), None)
        if owner is not None:
            raise DuplicateQrError(f"QR {code} was already scanned for slot {owner} in this run")
        slot.qr = code
        seen = self.qr_history().get(code)
        slot.qr_note = f"seen before: {seen}" if seen else ""
        slot.stage, slot.progress = "Done", 100
        notify(replace(slot, log=[]))
        return slot

    def skip_qr(self, run: BenchRun, slot_number: int, *, on_update: Listener | None = None) -> None:
        slot = run.slots[slot_number]
        if not slot.qr:
            slot.qr = NOT_SCANNED
            slot.stage, slot.progress = "Done", 100
            (on_update or (lambda _slot: None))(replace(slot, log=[]))

    def complete_qr(self, run: BenchRun, *, on_update: Listener | None = None) -> BenchRun:
        """End QR scanning (unscanned slots: NOT SCANNED) and record the run."""
        for slot in run.qr_slots:
            if not slot.qr:
                slot.qr = NOT_SCANNED
        run.qr_done = True
        return self._finish(run, on_update or (lambda _slot: None))

    def qr_history(self) -> dict[str, str]:
        """QRs recorded by earlier bench runs (from the results CSV)."""
        with self._lock:
            if self._qr_history is None:
                history: dict[str, str] = {}
                path = self._results_dir / "stock_bench_results.csv"
                try:
                    with path.open(newline="", encoding="utf-8") as handle:
                        for row in csv.DictReader(handle):
                            qr = (row.get("qr") or "").strip().upper()
                            if qr and qr != NOT_SCANNED:
                                history.setdefault(qr, f"run {row.get('run_id')} slot {row.get('slot')}")
                except (OSError, csv.Error):
                    pass
                self._qr_history = history
            return self._qr_history

    def write_qr_test(self, scans: dict[int, str]) -> Path:
        """Scanner test without programming: append the scans to a CSV."""
        with self._lock:
            self._results_dir.mkdir(parents=True, exist_ok=True)
            path = self._results_dir / "qr_scan_test.csv"
            new_file = not path.exists()
            stamp = datetime.now().isoformat(timespec="seconds")
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(["scanned_at", "slot", "qr"])
                for slot_number, code in sorted(scans.items()):
                    writer.writerow([stamp, slot_number, code])
            return path

    # ------------------------------------------------------------------

    def _program_slot(self, hardware: BenchHardware, settings: BenchSettings, slot: SlotRun, token: CancellationToken, notify: Listener) -> None:
        started = time.monotonic()

        def step(stage: str, progress: int) -> None:
            slot.stage, slot.progress = stage, progress
            notify(replace(slot, log=[]))

        outcome = self._flash_write_verify(hardware, settings, slot, MacAddress.parse(slot.mac), token, step, "")
        slot.flash, slot.mac_write = outcome.flash, outcome.mac_write
        slot.readback_mac, slot.readback = outcome.readback_mac, outcome.readback
        slot.message = outcome.message
        slot.seconds = round(time.monotonic() - started, 1)
        slot.stage = "Programmed" if slot.readback_ok else "Stopped"
        slot.progress = 90 if slot.readback_ok else 100
        notify(replace(slot, log=[]))

    def _flash_write_verify(self, hardware: BenchHardware, settings: BenchSettings, slot: SlotRun, mac: MacAddress, token: CancellationToken, step, label: str) -> "_Programmed":
        """Flash, write the MAC and read it back; never raises."""
        out = _Programmed()
        try:
            if settings.two_step:
                step("Flashing firmware", 10)
                result = hardware.flash(slot.com_port, token)
                self._record(slot, f"{label}FLASH", result.process)
                out.flash = result.outcome.value
                if not result.succeeded:
                    out.message = result.message
                    return out

                step("Settling after reset", 45)
                _sleep(settings.settle_s, token)

                step("Writing MAC", 55)
                result = hardware.write_mac(slot.com_port, mac, token)
                self._record(slot, f"{label}WRITE MAC", result.process)
                out.mac_write = result.outcome.value
            else:
                step("Flashing firmware + MAC", 10)
                result = hardware.flash_with_mac(slot.com_port, mac, token)
                self._record(slot, f"{label}FLASH+MAC", result.process)
                out.flash = out.mac_write = result.outcome.value

            if not result.succeeded:
                out.message = result.message
                return out

            step("Reading back MAC", 80)
            readback = hardware.read_mac(slot.com_port, token)
            self._record(slot, f"{label}READ MAC", readback.process)
            verification = compare_readback(expected=mac, readback=readback)
            out.readback_mac = str(readback.mac) if readback.mac else ""
            out.readback = {
                IdentityOutcome.MATCH: "MATCH",
                IdentityOutcome.MISMATCH: "MISMATCH",
                IdentityOutcome.NOT_VERIFIED: "NOT VERIFIED",
            }[verification.outcome]
            if not verification.matched:
                out.message = verification.message
        except Exception as exc:  # noqa: BLE001 - one slot must not stop the others
            logger.exception("Bench slot %s failed", slot.slot_number)
            out.message = f"{type(exc).__name__}: {exc}"
        return out

    def _rf(self, run: BenchRun, token: CancellationToken, notify: Listener) -> None:
        verified = [slot for slot in run.slots.values() if slot.readback_ok]
        for slot in run.slots.values():
            if not slot.readback_ok:
                slot.rf = "NOT TESTED"

        if run.rf_mode is RfMode.SKIP or not verified:
            for slot in verified:
                slot.rf = "SKIPPED"
            return

        if run.rf_mode is RfMode.MANUAL:
            for slot in verified:
                slot.rf, slot.stage = "AWAITING OPERATOR", "RF: operator check"
                notify(replace(slot, log=[]))
            return

        # Golden Rig: the modules are still in boot (download) mode after
        # programming. The operator resets them into application mode and
        # then starts the RF test (run_golden_rig_rf).
        for slot in verified:
            slot.rf, slot.stage = "AWAITING RESET", "Reset to application mode, then RUN RF TEST"
            notify(replace(slot, log=[]))

    def _finish(self, run: BenchRun, notify: Listener) -> BenchRun:
        for slot in run.slots.values():
            slot.verdict = _verdict(slot, run)

        if run.rf_pending:
            for slot in run.slots.values():
                notify(replace(slot, log=[]))
            return run

        if run.pricol and run.pricol_step is PricolStep.NOT_REACHED:
            run.pricol_step = PricolStep.PROGRAM if run.pricol_candidates and not run.cancelled else PricolStep.DONE
            for slot in run.slots.values():
                slot.verdict = _verdict(slot, run)
        if run.pricol_pending:
            waiting = {
                PricolStep.PROGRAM: (run.pricol_candidates, "Boot mode + reset, then PROGRAM PRICOL FIRMWARE"),
                PricolStep.APP: (run.app_candidates, "Application mode + power cycle, then RUN APP TEST"),
                PricolStep.DFU: (run.dfu_candidates, "Reset, then RUN DFU TEST"),
            }[run.pricol_step]
            for slot in run.slots.values():
                if slot in waiting[0]:
                    slot.stage, slot.progress = waiting[1], 92
                elif slot.verdict and "PENDING" not in slot.verdict:
                    slot.stage, slot.progress = "Done", 100
                notify(replace(slot, log=[]))
            return run

        waiting_for_qr = run.awaiting_qr
        for slot in run.slots.values():
            if waiting_for_qr and slot in run.qr_slots and not slot.qr:
                slot.stage, slot.progress = "Scan QR", 95
            else:
                slot.stage, slot.progress = "Done", 100
            notify(replace(slot, log=[]))

        if not waiting_for_qr:
            run.finished_at = datetime.now()
            self._write_results(run)
        return run

    # ------------------------------------------------------------------

    @staticmethod
    def _record(slot: SlotRun, label: str, process: ProcessResult) -> None:
        slot.log.append(
            f"=== {label} | {process.outcome.value} | rc={process.return_code} | {process.duration_seconds:.1f}s\n"
            f"$ mpcli {' '.join(sanitise_command(process.command))}\n"
            f"{process.stdout.rstrip()}\n{process.stderr.rstrip()}\n"
        )

    def _write_results(self, run: BenchRun) -> None:
        with self._lock:
            self._results_dir.mkdir(parents=True, exist_ok=True)
            run.csv_path = self._results_dir / "stock_bench_results.csv"
            run.log_dir = self._results_dir / run.run_id
            run.log_dir.mkdir(parents=True, exist_ok=True)

            _retire_old_csv(run.csv_path)
            new_file = not run.csv_path.exists()
            with run.csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(CSV_HEADER)
                pricol_firmware = run.pricol_settings.firmware.name if run.pricol else ""
                for slot in sorted(run.slots.values(), key=lambda item: item.slot_number):
                    writer.writerow([
                        run.run_id, run.finished_at.isoformat(timespec="seconds"),
                        "STOCK_AND_PRICOL" if run.pricol else "STOCK_ONLY", slot.slot_number,
                        slot.com_port, slot.mac, slot.flash, slot.mac_write, slot.readback_mac,
                        slot.readback, run.rf_mode.value, slot.rf, slot.rf_detail,
                        slot.pricol_mac, slot.pricol_flash, slot.pricol_mac_write, slot.pricol_readback_mac, slot.pricol_readback,
                        slot.app, slot.app_detail, slot.dfu, slot.dfu_detail,
                        slot.qr, slot.qr_note, slot.verdict, slot.seconds, slot.message,
                        run.firmware.name, run.firmware_sha256, pricol_firmware, run.pricol_firmware_sha256,
                    ])
            if self._qr_history is not None:
                for slot in run.slots.values():
                    if slot.qr and slot.qr != NOT_SCANNED:
                        self._qr_history.setdefault(slot.qr, f"run {run.run_id} slot {slot.slot_number}")

            for slot in run.slots.values():
                path = run.log_dir / f"slot{slot.slot_number}_{slot.com_port}.log"
                path.write_text("".join(slot.log) or "(no MPCLI output)\n", encoding="utf-8")


def _retire_old_csv(path: Path) -> None:
    """A results file from an older bench version (other columns) is
    renamed, so new rows never land under the wrong headings."""
    if not path.exists():
        return
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), None)
    except (OSError, csv.Error):
        return
    if header and header != CSV_HEADER:
        path.rename(path.with_name(f"{path.stem}_before_{datetime.now():%Y%m%d-%H%M%S}{path.suffix}"))


@dataclass(slots=True)
class _Programmed:
    flash: str = "-"
    mac_write: str = "-"
    readback_mac: str = ""
    readback: str = "-"
    message: str = ""


def _overlap(a_start: MacAddress, a_end: MacAddress, b_start: MacAddress, b_end: MacAddress) -> bool:
    return a_start.value <= b_end.value and b_start.value <= a_end.value


def _verdict(slot: SlotRun, run: BenchRun) -> str:
    if slot.aborted:
        return "ABORTED"
    stock = _stock_verdict(slot, run)
    if not run.pricol or not stock.startswith("PASS"):
        return stock
    # Stock + Pricol: Pricol programming, then PRICOLAPP and PRICOLDFU.
    if not slot.pricol_ok:
        if slot.pricol_flash in {"-", "NOT RUN"}:
            return "PRICOL PENDING" if run.pricol_step in (PricolStep.NOT_REACHED, PricolStep.PROGRAM) else "PRICOL NOT RUN"
        return "PRICOL FAIL"
    tests = (slot.app, slot.dfu)
    if "FAIL" in tests:
        return "FUNCTIONAL FAIL"
    if "ERROR" in tests:
        return "FUNCTIONAL ERROR"
    if "NOT TESTED" in tests:
        return "NOT TESTED"
    if "-" in tests:
        return "TEST PENDING" if run.pricol_pending else "NOT TESTED"
    return stock                                   # PASS or PASS (no RF)


def _stock_verdict(slot: SlotRun, run: BenchRun) -> str:
    if run.cancelled and not slot.readback_ok:
        return "CANCELLED"
    if not slot.readback_ok:
        return "FAIL"
    if slot.rf == "PASS":
        return "PASS"
    if slot.rf in {"SKIPPED", "NOT TESTED"}:
        return "PASS (no RF)"
    if slot.rf in {"AWAITING OPERATOR", "AWAITING RESET"}:
        return "RF PENDING"
    return f"RF {slot.rf}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sleep(seconds: float, token: CancellationToken) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        token.raise_if_cancelled()
        time.sleep(0.05)
