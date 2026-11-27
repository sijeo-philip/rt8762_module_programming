"""Process timing log: how long each production step takes.

Written to its own file (logs/stationapp_timing.log, see logging_setup)
and nowhere else, so cycle times can be read without the rest of the
station log. For each tray it records:

    * every slot's run of each process (programming, readback, RF test,
      APP/DFU test), retries included, with its outcome;
    * the whole tray's run of each process (the slots run in parallel,
      so this is the time the operator actually waits);
    * on COMPLETE BATCH, a summary: each process's tray time, the total
      time from START PROGRAMMING to COMPLETE BATCH, and the part of that
      total spent on operator handling between the steps.

Durations use a monotonic clock, so a change of the PC's clock never
distorts them. Timing state is in memory only: after an application
restart mid-tray the summary says the start was not seen.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

TIMING_LOGGER_NAME = "stationapp.timing"
TIMING_LOG_FILE = "stationapp_timing.log"

# Process names, as the operator sees the steps.
STOCK_PROGRAM = "STOCK PROGRAM"
STOCK_READBACK = "STOCK READBACK"
RF_TEST = "RF TEST"
PRICOL_PROGRAM = "PRICOL PROGRAM"
PRICOL_READBACK = "PRICOL READBACK"
APP_TEST = "APP TEST"
DFU_TEST = "DFU TEST"
QR_SCAN = "QR SCAN"
RETRY = "RETRY"


@dataclass
class _TrayClock:
    started: float | None  # START PROGRAMMING, None if not seen
    slots: tuple[int, ...] = ()
    # Total tray time per process, in the order first run.
    processes: dict[str, float] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    restarts: int = 0


class ProcessTiming:
    """Thread-safe: slots report from their own worker threads."""

    def __init__(self, logger: logging.Logger | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._log = logger or logging.getLogger(TIMING_LOGGER_NAME)
        self._clock = clock
        self._lock = threading.Lock()
        self._trays: dict[str, _TrayClock] = {}

    def now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------
    # Tray lifecycle
    # ------------------------------------------------------------------

    def programming_started(self, batch_id: str, slots: Iterable[int], *, chip_erase: bool | None = None) -> None:
        """The operator started Stock programming. A restarted tray keeps
        its first start, so the total covers every attempt."""
        slots = tuple(sorted(slots))
        with self._lock:
            tray = self._trays.get(batch_id)
            first = tray is None or tray.started is None
            if tray is None:
                tray = self._trays[batch_id] = _TrayClock(started=None)
            if first:
                tray.started = self._clock()
            tray.slots = slots
        note = "clock started" if first else "again after a tray restart; the total still counts from the first start"
        if chip_erase is not None:
            note += " | chip erase " + ("ON" if chip_erase else "OFF")
        self._write(batch_id, "START PROGRAMMING", _tray_scope(slots), None, note)

    def restarted(self, batch_id: str, attempt: int) -> None:
        with self._lock:
            tray = self._tray(batch_id)
            tray.restarts += 1
            tray.marks.clear()
        self._write(batch_id, "TRAY RESTARTED", f"attempt {attempt}", None, "every step runs again")

    def aborted(self, batch_id: str, reason: str) -> None:
        with self._lock:
            tray = self._trays.pop(batch_id, None)
            elapsed = None if tray is None or tray.started is None else self._clock() - tray.started
        self._write(batch_id, "BATCH ABORTED", "TRAY", elapsed, reason)

    def completed(self, batch_id: str, dispositions: Mapping[int, str]) -> None:
        """COMPLETE BATCH pressed and the tray committed: write the summary."""
        with self._lock:
            tray = self._trays.pop(batch_id, None) or _TrayClock(started=None)
            elapsed = None if tray.started is None else self._clock() - tray.started
            processes = dict(tray.processes)
            restarts = tray.restarts

        counts: dict[str, int] = {}
        for value in dispositions.values():
            counts[value] = counts.get(value, 0) + 1
        result = ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "-"

        lines = [f"===== BATCH SUMMARY | batch={batch_id} | slots={_slot_list(dispositions)} | {result} | restarts={restarts} ====="]
        for name, seconds in processes.items():
            lines.append(f"  {name:<18} {_duration(seconds)}")
        busy = sum(processes.values())
        lines.append(f"  {'All processes':<18} {_duration(busy)}")
        if elapsed is None:
            lines.append("  TOTAL              unknown: START PROGRAMMING was not seen since the application started")
        else:
            lines.append(f"  {'Operator handling':<18} {_duration(max(elapsed - busy, 0.0))}  (between steps: mode changes, resets, button presses)")
            lines.append(f"  {'TOTAL':<18} {_duration(elapsed)}  (START PROGRAMMING -> COMPLETE BATCH)")
        self._emit("\n".join(lines))

    # ------------------------------------------------------------------
    # Processes
    # ------------------------------------------------------------------

    @contextmanager
    def tray_process(self, batch_id: str, process: str, slots: Iterable[int]) -> Iterator[None]:
        """Time one process over the whole tray (all its slots together)."""
        slots = tuple(sorted(slots))
        if not slots:
            yield
            return
        started = self._clock()
        note = "done"
        try:
            yield
        except BaseException:
            note = "stopped by an error"
            raise
        finally:
            self._add(batch_id, process, self._clock() - started, _tray_scope(slots), note)

    def slot(self, batch_id: str, process: str, slot: int, seconds: float, outcome: str, *, retry: int = 0) -> None:
        """One slot's run of a process."""
        scope = f"slot {slot}" + (f" (retry {retry})" if retry else "")
        self._write(batch_id, process, scope, seconds, outcome)

    def mark(self, batch_id: str, process: str) -> None:
        """Start of a process timed from one event to another (QR scan)."""
        with self._lock:
            self._tray(batch_id).marks.setdefault(process, self._clock())

    def mark_done(self, batch_id: str, process: str, slots: Iterable[int]) -> None:
        with self._lock:
            started = self._tray(batch_id).marks.pop(process, None)
        if started is not None:
            self._add(batch_id, process, self._clock() - started, _tray_scope(tuple(sorted(slots))), "done")

    # ------------------------------------------------------------------

    def _add(self, batch_id: str, process: str, seconds: float, scope: str, note: str) -> None:
        with self._lock:
            tray = self._tray(batch_id)
            tray.processes[process] = tray.processes.get(process, 0.0) + seconds
        self._write(batch_id, process, scope, seconds, note)

    def _tray(self, batch_id: str) -> _TrayClock:
        """Caller holds the lock."""
        tray = self._trays.get(batch_id)
        if tray is None:
            tray = self._trays[batch_id] = _TrayClock(started=None)
        return tray

    def _write(self, batch_id: str, process: str, scope: str, seconds: float | None, note: str) -> None:
        took = _duration(seconds) if seconds is not None else "-"
        self._emit(f"batch={batch_id} | {process:<17} | {scope:<32} | {took:>18} | {note}")

    def _emit(self, text: str) -> None:
        try:
            self._log.info(text)
        except Exception:  # noqa: BLE001 - timing must never stop production
            pass


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)} min {rest:04.1f} s ({seconds:.1f} s)"


def _slot_list(slots: Iterable[int]) -> str:
    return ",".join(str(number) for number in sorted(slots)) or "-"


def _tray_scope(slots: tuple[int, ...]) -> str:
    return f"TRAY ({len(slots)} slots: {_slot_list(slots)})"
