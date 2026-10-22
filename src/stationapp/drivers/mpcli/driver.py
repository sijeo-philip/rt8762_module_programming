"""High-level typed interface around Realtek MPCLI."""

from __future__ import annotations
import time
from collections.abc import Callable
from pathlib import Path
from stationapp.concurrency.cancellation import (CancellationToken)
from stationapp.domain.mac import MacAddress
from dataclasses import replace
from stationapp.drivers.mpcli.commands import (build_chip_erase_command, build_flash_command, build_read_mac_command, build_reboot_command, build_version_command, build_set_mac_command, build_flash_with_mac_command)
from stationapp.drivers.mpcli.configuration import (ERASE_KEEP_FIRST_4K, ERASE_NONE, MpCliProgrammingProfile)
from stationapp.drivers.mpcli.errors import (MpCliInvalidCommand)
from stationapp.drivers.mpcli.process import (MpCliProcessRunner)
from stationapp.drivers.mpcli.types import (ProcessResult)
from stationapp.drivers.mpcli.parsers import (parse_mac_readback, parse_programming_result)
from stationapp.drivers.mpcli.types import (MacReadbackResult, ProcessResult,ProgrammingResult)

class MpCliDriver:
    """Typed MPCLI operations used by station services."""

    def __init__(self, process_runner: MpCliProcessRunner) -> None:
        self._runner = process_runner

    def version(self, *, timeout_seconds: float = 5.0) -> ProcessResult:

        return self._runner.run(build_version_command(), timeout_seconds=timeout_seconds)

    def flash_with_mac(self, *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile, timeout_seconds: float, cancellation_token: CancellationToken | None = None) -> ProcessResult:
        """Flash packed image and write one MAC."""
        self._require_image(profile.image_packet)
        arguments = build_flash_with_mac_command(com_port=com_port, mac=mac, profile=profile)

        return self._runner.run(arguments, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)

    def read_mac(self, *, com_port: str, baud: int = 1_000_000, timeout_seconds: float = 10.0, cancellation_token: CancellationToken | None = None) -> ProcessResult:
        """Request EUID/MAC read-back."""

        return self._runner.run(build_read_mac_command(com_port=com_port, baud=baud), timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)

    def reboot(self, *, com_port: str, baud: int = 1_000_000, timeout_seconds: float = 10.0) -> ProcessResult:

        return self._runner.run(build_reboot_command(com_port=com_port, baud=baud), timeout_seconds=timeout_seconds)

    @staticmethod
    def _require_image(image_packet: Path) -> None:

        if not image_packet.is_file():
            raise MpCliInvalidCommand(f"MPCLI image packet does not exist: {image_packet}")


    def erase_chip(self, *, com_port: str, profile: MpCliProgrammingProfile, timeout_seconds: float = 60.0, cancellation_token: CancellationToken | None = None) -> ProgrammingResult:
        """Chip erase before flashing (profile.chip_erase); never raises."""
        args = build_chip_erase_command(com_port=com_port, baud=profile.baud, keep_first_4k=profile.chip_erase == ERASE_KEEP_FIRST_4K)
        result = parse_programming_result(self._runner.run(args, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token))
        if not result.succeeded:
            result = replace(result, message=f"Chip erase failed: {result.message}")
        return result

    def _erase_first(self, com_port: str, profile: MpCliProgrammingProfile, cancellation_token: CancellationToken | None, on_phase: Callable[[str], None] | None, settle_seconds: float) -> ProgrammingResult | None:
        """Erase the chip if the profile asks for it.

        Returns the failed erase result (nothing may be flashed then), or
        None to continue.
        """
        if profile.chip_erase == ERASE_NONE:
            return None
        if on_phase is not None:
            on_phase(PHASE_ERASING)
        erase = self.erase_chip(com_port=com_port, profile=profile, cancellation_token=cancellation_token)
        if not erase.succeeded:
            return erase
        _settle(settle_seconds, cancellation_token)
        return None

    def program(self, *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile, timeout_seconds: float, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None, settle_seconds: float = 2.0) -> ProgrammingResult:
        """Chip erase, then flash image + MAC; structured outcome."""

        failed_erase = self._erase_first(com_port, profile, cancellation_token, on_phase, settle_seconds)
        if failed_erase is not None:
            return failed_erase
        if on_phase is not None:
            on_phase(PHASE_FLASHING)
        raw = self.flash_with_mac(com_port=com_port, mac=mac, profile=profile, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)
        return parse_programming_result(raw)

    def read_mac_structured(self, *, com_port: str, baud: int = 1_000_000, timeout_seconds: float = 10.0, cancellation_token: CancellationToken | None = None) -> MacReadbackResult:
        """Read MAC and return parsed structured result."""

        raw = self.read_mac(com_port=com_port, baud=baud,timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)
        return parse_mac_readback(raw)

    def flash(self, *, com_port: str, profile: MpCliProgrammingProfile, timeout_seconds: float = 60.0, cancellation_token: CancellationToken | None = None) -> ProcessResult:
        self._require_image(profile.image_packet)
        args = build_flash_command(com_port=com_port, profile=profile)

        return self._runner.run(args, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)

    def set_mac(self, *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile, timeout_seconds: float = 15.0, cancellation_token: CancellationToken | None = None) -> ProcessResult:

        args = build_set_mac_command(com_port=com_port, mac=mac, profile=profile, reboot=True)
        return self._runner.run(args, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)

    def program_sequenced(self, *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile, timeout_seconds: float, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None, settle_seconds: float = 2.0) -> ProgrammingResult:
        """Validated sequence: chip erase, flash firmware, settle, write MAC.

        Mirrors scripts/program_one_real_dut.py, the sequence proven on
        real DUTs, with the chip erase (profile.chip_erase) first. An erase
        or flash failure is returned before any MAC is written; the caller
        still treats the reserved MAC conservatively.
        """
        failed_erase = self._erase_first(com_port, profile, cancellation_token, on_phase, settle_seconds)
        if failed_erase is not None:
            return failed_erase
        if on_phase is not None:
            on_phase(PHASE_FLASHING)

        flash = self.program_firmware_only(com_port=com_port, profile=profile, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)
        if not flash.succeeded:
            return flash

        _settle(settle_seconds, cancellation_token)

        if on_phase is not None:
            on_phase(PHASE_WRITING_MAC)

        raw = self.set_mac(com_port=com_port, mac=mac, profile=profile, cancellation_token=cancellation_token)
        return parse_programming_result(raw)

    def program_firmware_only(self, *, com_port: str, profile: MpCliProgrammingProfile, timeout_seconds: float, cancellation_token: CancellationToken | None = None) -> ProgrammingResult:
        raw = self.flash(com_port=com_port, profile=profile, timeout_seconds=timeout_seconds, cancellation_token=cancellation_token)
        return parse_programming_result(raw)


def _settle(seconds: float, cancellation_token: CancellationToken | None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancellation_token is not None and cancellation_token.is_cancelled:
            break
        time.sleep(0.05)


PHASE_ERASING = "ERASING"
PHASE_FLASHING = "FLASHING"
PHASE_WRITING_MAC = "WRITING_MAC"
