"""Test MAC range for bench runs.

Bench runs do not use the production MAC cache. The engineer enters a
test range; every MAC handed out is recorded as used *before* it is
programmed and is never handed out again, even if programming fails -
the same rule production applies to uncertain MACs.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from stationapp.domain.mac import MacAddress


class MacRangeError(ValueError):
    pass


class BenchMacPool:
    def __init__(self, state_file: Path) -> None:
        self._state_file = Path(state_file)
        self._lock = threading.Lock()
        self._start: MacAddress | None = None
        self._end: MacAddress | None = None
        self._used: set[int] = set()
        self._load()

    @property
    def start(self) -> MacAddress | None:
        return self._start

    @property
    def end(self) -> MacAddress | None:
        return self._end

    def set_range(self, start: str, end: str) -> None:
        first = MacAddress.parse(start)
        last = MacAddress.parse(end)
        if last.value < first.value:
            raise MacRangeError("End MAC must not be lower than start MAC")
        for mac in (first, last):
            # Bit 0 of the first octet marks a multicast address, which a
            # device must never use as its own address.
            if (mac.value >> 40) & 0x01:
                raise MacRangeError(f"{mac} is a multicast address")
        with self._lock:
            self._start, self._end = first, last
            self._save()

    def remaining(self) -> int:
        with self._lock:
            if self._start is None or self._end is None:
                return 0
            total = self._end.value - self._start.value + 1
            used = sum(1 for value in self._used if self._start.value <= value <= self._end.value)
            return total - used

    def peek_next(self) -> MacAddress | None:
        with self._lock:
            return self._next_unused()

    def allocate(self, count: int) -> list[MacAddress]:
        """Reserve `count` unused MACs and persist them as used."""
        with self._lock:
            if self._start is None or self._end is None:
                raise MacRangeError("Enter a test MAC range first")
            allocated: list[MacAddress] = []
            for _ in range(count):
                mac = self._next_unused()
                if mac is None:
                    raise MacRangeError(
                        f"Test MAC range exhausted: {len(allocated)} of {count} available"
                    )
                self._used.add(mac.value)
                allocated.append(mac)
            self._save()
            return allocated

    def _next_unused(self) -> MacAddress | None:
        if self._start is None or self._end is None:
            return None
        value = self._start.value
        while value <= self._end.value:
            if value not in self._used:
                return MacAddress(value)
            value += 1
        return None

    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if data.get("range_start"):
                self._start = MacAddress.parse(data["range_start"])
                self._end = MacAddress.parse(data["range_end"])
            self._used = {MacAddress.parse(item).value for item in data.get("used", [])}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MacRangeError(f"Bench MAC state file {self._state_file} is unreadable: {exc}") from exc

    def _save(self) -> None:
        payload = {
            "range_start": str(self._start) if self._start else None,
            "range_end": str(self._end) if self._end else None,
            "used": [str(MacAddress(value)) for value in sorted(self._used)],
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self._state_file)
