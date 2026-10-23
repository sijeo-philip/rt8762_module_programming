"""Slot-to-COM-port binding from reference modules.

The Supervisor places one reference module per slot whose MAC names the
slot: 22:22:22:22:22:01 in slot 1, ... 22:22:22:22:22:08 in slot 8 (the
prefix is configurable, the last byte is the slot number). The station
reads the MAC through every detected COM port and proposes:

    port whose MAC is <prefix>:NN  ->  slot NN

Anything that does not fit is reported, never guessed: a port with no
readable MAC, a MAC that is not a reference MAC, a slot number outside the
jig, or the same slot seen on two ports (both are left out).

Qt-free; used by Station configuration -> Jig slot binding and by the
bench tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from stationapp.domain.mac import MacAddress

logger = logging.getLogger(__name__)

DEFAULT_REFERENCE_PREFIX = "22:22:22:22:22"


def normalise_prefix(prefix: str) -> str:
    """'22:22:22:22:22' (five bytes, colon separated, upper case)."""
    text = prefix.strip().replace("-", ":").upper()
    parts = text.split(":")
    if len(parts) != 5 or not all(len(part) == 2 and all(c in "0123456789ABCDEF" for c in part) for part in parts):
        raise ValueError(f"Reference MAC prefix must be five bytes like 22:22:22:22:22, got {prefix!r}")
    return text


def reference_slot(mac: MacAddress | str, prefix: str = DEFAULT_REFERENCE_PREFIX) -> int | None:
    """Slot number named by a reference MAC, or None if it is not one."""
    colon = MacAddress.parse(mac).colon
    head, _, last = colon.rpartition(":")
    if head != normalise_prefix(prefix):
        return None
    return int(last, 16) or None


@dataclass(frozen=True, slots=True)
class PortReading:
    device: str                      # COM port, e.g. COM7
    stable_key: str | None = None    # USB identity (station app), None on the bench
    mac: str | None = None           # MAC read, colon form
    error: str | None = None         # why no MAC could be read
    slot: int | None = None          # slot named by the MAC


@dataclass(frozen=True, slots=True)
class AutoBindPlan:
    jig_positions: int
    readings: tuple[PortReading, ...]
    assignments: dict[int, PortReading] = field(default_factory=dict)   # slot -> port
    problems: tuple[str, ...] = ()

    @property
    def missing_slots(self) -> tuple[int, ...]:
        return tuple(slot for slot in range(1, self.jig_positions + 1) if slot not in self.assignments)

    @property
    def complete(self) -> bool:
        return not self.missing_slots and not self.problems

    def summary(self) -> str:
        found = len(self.assignments)
        text = f"{found}/{self.jig_positions} slots identified"
        if self.missing_slots:
            text += f"; no reference module found for slot {', '.join(map(str, self.missing_slots))}"
        if self.problems:
            text += f"; {len(self.problems)} problem(s)"
        return text


def build_plan(readings: Iterable[PortReading], *, jig_positions: int, prefix: str = DEFAULT_REFERENCE_PREFIX) -> AutoBindPlan:
    readings = tuple(sorted(readings, key=lambda item: _port_order(item.device)))
    problems: list[str] = []
    by_slot: dict[int, list[PortReading]] = {}
    resolved: list[PortReading] = []
    for reading in readings:
        if reading.mac is None:
            problems.append(f"{reading.device}: no MAC read ({reading.error or 'no answer'})")
            resolved.append(reading)
            continue
        slot = reference_slot(reading.mac, prefix)
        if slot is None:
            problems.append(f"{reading.device}: MAC {reading.mac} is not a reference MAC ({normalise_prefix(prefix)}:NN)")
        elif slot > jig_positions:
            problems.append(f"{reading.device}: reference MAC {reading.mac} names slot {slot}, but the jig has {jig_positions} slots")
            slot = None
        reading = PortReading(reading.device, reading.stable_key, reading.mac, reading.error, slot)
        resolved.append(reading)
        if slot is not None:
            by_slot.setdefault(slot, []).append(reading)

    assignments: dict[int, PortReading] = {}
    for slot, found in sorted(by_slot.items()):
        if len(found) > 1:
            ports = " and ".join(item.device for item in found)
            problems.append(f"Slot {slot}: reference MAC read on {ports} - check the reference modules; slot left out")
            continue
        assignments[slot] = found[0]
    return AutoBindPlan(jig_positions, tuple(resolved), assignments, tuple(problems))


MacReader = Callable[[str], object]   # COM port -> MacReadbackResult-like (.mac, .message)


def read_reference_modules(devices: Iterable[tuple[str, str | None]], read_mac: MacReader, *, jig_positions: int, prefix: str = DEFAULT_REFERENCE_PREFIX) -> AutoBindPlan:
    """Read the MAC on every (device, stable_key) in parallel and plan."""
    devices = tuple(devices)

    def read(item: tuple[str, str | None]) -> PortReading:
        device, stable_key = item
        try:
            result = read_mac(device)
        except Exception as exc:  # noqa: BLE001 - one port must not stop the others
            logger.exception("Reference MAC read failed on %s", device)
            return PortReading(device, stable_key, error=f"{type(exc).__name__}: {exc}")
        mac = getattr(result, "mac", None)
        if mac is None:
            return PortReading(device, stable_key, error=getattr(result, "message", "") or "MAC not readable")
        return PortReading(device, stable_key, mac=MacAddress.parse(mac).colon)

    if not devices:
        return build_plan((), jig_positions=jig_positions, prefix=prefix)
    with ThreadPoolExecutor(max_workers=len(devices), thread_name_prefix="reference-mac") as pool:
        readings = list(pool.map(read, devices))
    plan = build_plan(readings, jig_positions=jig_positions, prefix=prefix)
    logger.info("Reference module scan: %s", plan.summary())
    return plan


def _port_order(device: str) -> tuple[int, str]:
    digits = "".join(c for c in device if c.isdigit())
    return (int(digits) if digits else 0, device)
