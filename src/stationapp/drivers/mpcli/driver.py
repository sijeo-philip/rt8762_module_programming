"""High-level typed interface around Realtek MPCLI."""

from __future__ import annotations
from pathlib import Path
from stationapp.concurrency.cancellation import (CancellationToken)
from stationapp.domain.mac import MacAddress
from stationapp.drivers.mpcli.commands import (build_flash_command, build_read_mac_command, build_reboot_command, build_version_command, build_set_mac_command, build_flash_with_mac_command)
from stationapp.drivers.mpcli.configuration import (MpCliProgrammingProfile)
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
            raise MpCliInvalidCommand("MPCLI image packet does not exist: {image_packet}")


    def program(self, *, com_port: str, mac: MacAddress, profile: MpCliProgrammingProfile, timeout_seconds: float, cancellation_token: CancellationToken | None = None) -> ProgrammingResult:
        """Flash image + MAC and return structured outcome."""

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
