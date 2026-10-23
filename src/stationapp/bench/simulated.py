"""Simulated bench hardware for trying the tool without a jig."""

from __future__ import annotations

import time

from stationapp.concurrency.cancellation import CancellationToken
from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.parsers import parse_programming_result
from stationapp.drivers.mpcli.types import (
    MacReadbackOutcome,
    MacReadbackResult,
    ProcessOutcome,
    ProcessResult,
    ProgrammingOutcome,
    ProgrammingResult,
)


class SimulatedBenchHardware:
    """Pretends to be MPCLI. Ports listed in fail_ports fail to flash."""

    def __init__(self, *, delay_s: float = 1.0, fail_ports: frozenset[str] = frozenset(), reference_macs: dict[str, str] | None = None) -> None:
        self._delay = delay_s
        self._fail = {port.upper() for port in fail_ports}
        self._written: dict[str, MacAddress] = {}
        # MAC a port reports before anything is programmed (reference modules).
        self._reference = {port.upper(): MacAddress.parse(mac) for port, mac in (reference_macs or {}).items()}

    def erase(self, com_port: str, token: CancellationToken) -> ProgrammingResult:
        self._wait(token, 0.5)
        self._written.pop(com_port.upper(), None)
        process = ProcessResult(("-c", com_port, "-E", "-r"), ProcessOutcome.COMPLETED, 0, "chip erase -OK-", "", self._delay * 0.5)
        return parse_programming_result(process)

    def flash(self, com_port: str, token: CancellationToken) -> ProgrammingResult:
        self._wait(token, 2.0)
        failed = com_port.upper() in self._fail
        return self._result(("-P", "<image>", "-c", com_port), failed, "Simulated flash")

    def write_mac(self, com_port: str, mac: MacAddress, token: CancellationToken) -> ProgrammingResult:
        self._wait(token, 0.5)
        self._written[com_port.upper()] = mac
        return self._result(("-c", com_port, "-x", str(mac), "-r"), False, f"MAC written: {mac}")

    def flash_with_mac(self, com_port: str, mac: MacAddress, token: CancellationToken) -> ProgrammingResult:
        result = self.flash(com_port, token)
        if result.succeeded:
            self._written[com_port.upper()] = mac
        return result

    def read_mac(self, com_port: str, token: CancellationToken) -> MacReadbackResult:
        self._wait(token, 0.3)
        mac = self._written.get(com_port.upper()) or self._reference.get(com_port.upper())
        process = ProcessResult(("-c", com_port, "-I"), ProcessOutcome.COMPLETED, 0, f"BT Address: {mac.compact if mac else ''}", "", self._delay * 0.3)
        if mac is None:
            return MacReadbackResult(MacReadbackOutcome.NOT_FOUND, process, None, "No MAC in simulated output")
        return MacReadbackResult(MacReadbackOutcome.READ, process, mac, "Simulated readback")

    def _result(self, command, failed: bool, text: str) -> ProgrammingResult:
        # A failing port behaves like a module that does not answer:
        # MPCLI exits with ERR 6 during loadfw, as seen on real jigs.
        output = "\n".join((
            "CMD[2] loadfw --autodetect fw",
            "-ERR: 6-",
            "recv hci evt Fail!",
            "Detect FW Fail",
        )) if failed else text
        process = ProcessResult(tuple(command), ProcessOutcome.COMPLETED, 6 if failed else 0, output, "", self._delay)
        if failed:
            return parse_programming_result(process)
        return ProgrammingResult(ProgrammingOutcome.SUCCESS, process, "Simulated programming complete")

    def _wait(self, token: CancellationToken, factor: float) -> None:
        deadline = time.monotonic() + self._delay * factor
        while time.monotonic() < deadline:
            token.raise_if_cancelled()
            time.sleep(0.05)
