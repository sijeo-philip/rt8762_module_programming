"""Stock firmware bench run: flash -> write MAC -> read back -> optional RF.

Qt-free so it can be tested and scripted. One worker per selected slot,
as in production. Results go to a CSV (one row per slot per run) and to
per-slot MPCLI logs with the secret key redacted.
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


@dataclass(frozen=True, slots=True)
class BenchSettings:
    mpcli_path: Path
    firmware: Path
    product_id: str
    secret_key: str
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
    log: list[str] = field(default_factory=list)

    @property
    def readback_ok(self) -> bool:
        return self.readback == "MATCH"


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

    @property
    def awaiting_manual_rf(self) -> list[SlotRun]:
        return [slot for slot in self.slots.values() if slot.rf == "AWAITING OPERATOR"]


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
            product_id=settings.product_id,
            secret_key=settings.secret_key,
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
    def __init__(self, *, hardware_factory: HardwareFactory, mac_pool: BenchMacPool, results_dir: Path, rf_tester=None, station_id: str = "BENCH", jig_id: str = "BENCH") -> None:
        self._hardware_factory = hardware_factory
        self._mac_pool = mac_pool
        self._results_dir = Path(results_dir)
        self._rf_tester = rf_tester
        self._station_id = station_id
        self._jig_id = jig_id
        self._lock = threading.Lock()

    @property
    def golden_rig_available(self) -> bool:
        return self._rf_tester is not None

    # ------------------------------------------------------------------

    def run(self, settings: BenchSettings, slots: dict[int, str], rf_mode: RfMode, *, on_update: Listener | None = None, token: CancellationToken | None = None) -> BenchRun:
        if not slots:
            raise ValueError("Select at least one slot")
        ports = [port.strip().upper() for port in slots.values()]
        if len(set(ports)) != len(ports):
            raise ValueError("Each selected slot needs its own COM port")
        if not Path(settings.firmware).is_file():
            raise ValueError(f"Firmware image not found: {settings.firmware}")
        if rf_mode is RfMode.GOLDEN_RIG and self._rf_tester is None:
            raise ValueError("No approved Golden Rig is available for RF testing")

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

    # ------------------------------------------------------------------

    def _program_slot(self, hardware: BenchHardware, settings: BenchSettings, slot: SlotRun, token: CancellationToken, notify: Listener) -> None:
        started = time.monotonic()
        mac = MacAddress.parse(slot.mac)

        def step(stage: str, progress: int) -> None:
            slot.stage, slot.progress = stage, progress
            notify(replace(slot, log=[]))

        try:
            if settings.two_step:
                step("Flashing firmware", 10)
                result = hardware.flash(slot.com_port, token)
                self._record(slot, "FLASH", result.process)
                slot.flash = result.outcome.value
                if not result.succeeded:
                    slot.message = result.message
                    return

                step("Settling after reset", 45)
                _sleep(settings.settle_s, token)

                step("Writing MAC", 55)
                result = hardware.write_mac(slot.com_port, mac, token)
                self._record(slot, "WRITE MAC", result.process)
                slot.mac_write = result.outcome.value
            else:
                step("Flashing firmware + MAC", 10)
                result = hardware.flash_with_mac(slot.com_port, mac, token)
                self._record(slot, "FLASH+MAC", result.process)
                slot.flash = slot.mac_write = result.outcome.value

            if not result.succeeded:
                slot.message = result.message
                return

            step("Reading back MAC", 80)
            readback = hardware.read_mac(slot.com_port, token)
            self._record(slot, "READ MAC", readback.process)
            verification = compare_readback(expected=mac, readback=readback)
            slot.readback_mac = str(readback.mac) if readback.mac else ""
            slot.readback = {
                IdentityOutcome.MATCH: "MATCH",
                IdentityOutcome.MISMATCH: "MISMATCH",
                IdentityOutcome.NOT_VERIFIED: "NOT VERIFIED",
            }[verification.outcome]
            if not verification.matched:
                slot.message = verification.message
        except Exception as exc:  # noqa: BLE001 - one slot must not stop the others
            logger.exception("Bench slot %s failed", slot.slot_number)
            slot.message = f"{type(exc).__name__}: {exc}"
        finally:
            slot.seconds = round(time.monotonic() - started, 1)
            slot.stage = "Programmed" if slot.readback_ok else "Stopped"
            slot.progress = 90 if slot.readback_ok else 100
            notify(replace(slot, log=[]))

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

        for slot in verified:
            slot.stage = "Golden Rig RF test"
            notify(replace(slot, log=[]))
        try:
            report = self._rf_tester.run_test(
                batch_id=f"BENCH-{run.run_id}",
                station_id=self._station_id,
                jig_id=self._jig_id,
                targets=tuple(GoldenRigTarget(slot.slot_number, MacAddress.parse(slot.mac)) for slot in verified),
                cancel_event=token.event,
            )
        except Exception as exc:  # noqa: BLE001 - rig problems are reported, not fatal
            logger.exception("Golden Rig RF test failed")
            for slot in verified:
                slot.rf, slot.rf_detail = "ERROR", f"Golden Rig: {exc}"
            return

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

    def _finish(self, run: BenchRun, notify: Listener) -> BenchRun:
        for slot in run.slots.values():
            slot.verdict = _verdict(slot, run)
            if not run.awaiting_manual_rf:
                slot.stage, slot.progress = "Done", 100
            notify(replace(slot, log=[]))

        if not run.awaiting_manual_rf:
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

            new_file = not run.csv_path.exists()
            with run.csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow([
                        "run_id", "finished_at", "slot", "com_port", "mac", "flash", "mac_write",
                        "readback_mac", "readback", "rf_mode", "rf", "rf_detail", "verdict",
                        "seconds", "message", "firmware", "firmware_sha256",
                    ])
                for slot in sorted(run.slots.values(), key=lambda item: item.slot_number):
                    writer.writerow([
                        run.run_id, run.finished_at.isoformat(timespec="seconds"), slot.slot_number,
                        slot.com_port, slot.mac, slot.flash, slot.mac_write, slot.readback_mac,
                        slot.readback, run.rf_mode.value, slot.rf, slot.rf_detail, slot.verdict,
                        slot.seconds, slot.message, run.firmware.name, run.firmware_sha256,
                    ])

            for slot in run.slots.values():
                path = run.log_dir / f"slot{slot.slot_number}_{slot.com_port}.log"
                path.write_text("".join(slot.log) or "(no MPCLI output)\n", encoding="utf-8")


def _verdict(slot: SlotRun, run: BenchRun) -> str:
    if run.cancelled and not slot.readback_ok:
        return "CANCELLED"
    if not slot.readback_ok:
        return "FAIL"
    if slot.rf == "PASS":
        return "PASS"
    if slot.rf in {"SKIPPED", "NOT TESTED"}:
        return "PASS (no RF)"
    if slot.rf == "AWAITING OPERATOR":
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
