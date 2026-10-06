"""Runtime validation of jig serial-port topology."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stationapp.domain import slot
from stationapp.drivers.serial.types import (DiscoveredSerialPort)
from stationapp.services.slot_binding import (SlotBinding, SlotBindingConfiguration)

from stationapp.drivers.serial.discovery import (discover_stable_usb_ports)
from stationapp.services.slot_binding import (SlotBindingService)


class SlotTopologyStatus(str, Enum):
    """Current runtime state of one jig position."""

    READY = "READY"
    UNBOUND = "UNBOUND"
    MISSING = "MISSING"
    MOVED = "MOVED"

class SlotNotReadyError(RuntimeError):
    """Requested jig slot is not safe for operation."""


@dataclass(frozen=True, slots=True)
class ResolvedSlot:
    """Runtime resolution of one physical jig position."""

    slot_number: int
    status: SlotTopologyStatus

    binding: SlotBinding | None = None
    port: DiscoveredSerialPort | None = None

    message: str = ""

    @property
    def ready(self) -> bool:
        return self.status is SlotTopologyStatus.READY

    @property
    def com_port(self) -> str | None:
        if self.port is None:
            return None

        return self.port.device

    @property
    def stable_key(self) -> str | None:
        if self.binding is None:
            return None

        return self.binding.stable_key


@dataclass(frozen=True, slots=True)
class SerialTopology:
    """Complete runtime view of all jig serial positions."""

    slots: tuple[ResolvedSlot, ...]

    def resolve_slot(self, slot_number: int) -> ResolvedSlot:
        for slot in self.slots:
            if slot.slot_number == slot_number:
                return slot

        raise ValueError(f"Unknown jig slot {slot_number}")

    @property
    def all_ready(self) -> bool:
        return all(slot.ready for slot in self.slots)

    @property
    def ready_count(self) -> int:
        return sum(1 for slot in self.slots if slot.ready)

    @property
    def failed_count(self) -> int:
        return len(self.slots) - self.ready_count

    def require_ready_slot(self, slot_number: int) -> ResolvedSlot:

        slot = self.resolve_slot(slot_number)
        if not slot.ready:
            raise SlotNotReadyError(
                f"Slot {slot_number} is "
                f"{slot.status.value}: "
                f"{slot.message}"
            )

        return slot

    def require_com_port(self, slot_number: int) -> str:
        slot = self.require_ready_slot(slot_number)
        assert slot.com_port is not None
        return slot.com_port


class SerialTopologyService:
    """Matches saved jig bindings against currently discovered ports."""

    def resolve(self, *, bindings: SlotBindingConfiguration, discovered_ports: tuple[DiscoveredSerialPort, ...]) -> SerialTopology:

        ports_by_key = self._index_ports(discovered_ports)
        resolved: list[ResolvedSlot] = []
        for slot_number in range(1, bindings.jig_positions + 1):
            binding = bindings.get(slot_number)
            if binding is None:
                resolved.append(ResolvedSlot(slot_number=slot_number, status=(SlotTopologyStatus.UNBOUND),
                        message=(
                            f"Slot {slot_number} has "
                            "no configured serial binding."
                        ),
                    )
                )
                continue

            port = ports_by_key.get(binding.stable_key)

            if port is None:
                resolved.append(ResolvedSlot(slot_number=slot_number, status=(SlotTopologyStatus.MISSING),
                        binding=binding,
                        message=(
                            f"Expected serial interface "
                            f"{binding.stable_key} "
                            "was not detected."
                        ),
                    )
                )
                continue

            moved = self._is_moved(binding=binding, port=port)
            if moved:
                resolved.append(ResolvedSlot(slot_number=slot_number, status=(SlotTopologyStatus.MOVED),
                        binding=binding,
                        port=port,
                        message=(
                            f"Serial interface is present "
                            f"as {port.device}, but its USB "
                            "location differs from the "
                            "commissioned jig location."
                        ),
                    )
                )
                continue

            resolved.append(
                ResolvedSlot(slot_number=slot_number, status=(SlotTopologyStatus.READY),
                    binding=binding,
                    port=port,
                    message=(
                        f"Slot {slot_number} resolved "
                        f"to {port.device}."
                    ),
                )
            )

        return SerialTopology(
            slots=tuple(resolved)
        )

    @staticmethod
    def _index_ports(ports: tuple[DiscoveredSerialPort, ...]) -> dict[str, DiscoveredSerialPort]:

        result: dict[str, DiscoveredSerialPort] = {}
        for port in ports:
            key = port.stable_key
            if key in result:
                raise ValueError(
                    "Duplicate runtime serial "
                    f"identity detected: {key}"
                )
            result[key] = port
        return result

    @staticmethod
    def _is_moved(*, binding: SlotBinding, port: DiscoveredSerialPort) -> bool:
        """Detect adapter moved to another USB hub position.

        Only enforce location when both saved and runtime
        locations are available.
        """

        saved_location = binding.location
        current_location = (port.identity.location)
        if (saved_location is None or current_location is None):
            return False

        return (saved_location.strip().upper() != current_location.strip().upper())

class StationSerialTopologyService:
    """Application-facing topology service.

    Loads persistent slot bindings and performs
    fresh serial discovery every time refresh()
    is called.
    """

    def __init__(self, *, binding_service: SlotBindingService) -> None:
        self._binding_service = (binding_service)

        self._resolver = (SerialTopologyService())

    def refresh(self) -> SerialTopology:
        bindings = (self._binding_service.load())

        discovered = (discover_stable_usb_ports())

        return self._resolver.resolve(bindings=bindings, discovered_ports=discovered)

    