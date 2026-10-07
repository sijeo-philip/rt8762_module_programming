"""Per-slot operational eligibility.

Topology describes what hardware is present.
Eligibility decides whether a production operation may use that slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stationapp.services.serial_topology import (
    ResolvedSlot,
    SerialTopology,
    SlotTopologyStatus,
)

@dataclass(frozen=True, slots=True)
class EligibleSlotTarget:
    """Resolved physical slot ready for an operation."""

    slot_number: int
    com_port: str


class SlotEligibilityStatus(str, Enum):
    """Whether a slot may participate in station operations."""

    ELIGIBLE = "ELIGIBLE"

    BLOCKED_UNBOUND = "BLOCKED_UNBOUND"
    BLOCKED_MISSING = "BLOCKED_MISSING"
    BLOCKED_MOVED = "BLOCKED_MOVED"

@dataclass(frozen=True, slots=True)
class SlotEligibility:
    """Operational eligibility for one physical jig position."""

    slot_number: int
    status: SlotEligibilityStatus
    topology: ResolvedSlot
    reason: str

    @property
    def eligible(self) -> bool:
        return (
            self.status
            is SlotEligibilityStatus.ELIGIBLE
        )

    @property
    def com_port(self) -> str | None:
        if not self.eligible:
            return None

        return self.topology.com_port

@dataclass(frozen=True, slots=True)
class JigEligibility:
    """Eligibility state for every slot in one jig."""

    slots: tuple[SlotEligibility, ...]

    def get(self, slot_number: int) -> SlotEligibility:

        for slot in self.slots:
            if slot.slot_number == slot_number:
                return slot

        raise ValueError(f"Unknown jig slot {slot_number}")

    @property
    def eligible_slots(self) -> tuple[SlotEligibility, ...]:

        return tuple(slot for slot in self.slots if slot.eligible)

    @property
    def blocked_slots(self) -> tuple[SlotEligibility, ...]:

        return tuple(slot for slot in self.slots if not slot.eligible)

    @property
    def eligible_slot_numbers(self) -> tuple[int, ...]:

        return tuple(slot.slot_number for slot in self.eligible_slots)

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_slots)

    @property
    def blocked_count(self) -> int:
        return len(self.blocked_slots)

    @property
    def none_eligible(self) -> bool:
        return self.eligible_count == 0

    def require_eligible(self, slot_number: int) -> SlotEligibility:

        slot = self.get(slot_number)

        if not slot.eligible:
            raise RuntimeError(
                f"Slot {slot_number} is not eligible: "
                f"{slot.reason}"
            )

        return slot

    def require_com_port(self, slot_number: int) -> str:

        slot = self.require_eligible(slot_number)
        assert slot.com_port is not None
        return slot.com_port

    @property
    def operation_targets(self) -> tuple[EligibleSlotTarget, ...]:

        targets: list[EligibleSlotTarget] = []

        for slot in self.eligible_slots:
            assert slot.com_port is not None

            targets.append(
                EligibleSlotTarget(
                    slot_number=(
                        slot.slot_number
                    ),
                    com_port=slot.com_port,
                )
            )

        return tuple(targets)
    


class SlotEligibilityService:
    """Convert current topology into per-slot operational eligibility."""

    def evaluate(self, topology: SerialTopology) -> JigEligibility:

        results: list[SlotEligibility] = []
        for slot in topology.slots:
            results.append(self._evaluate_slot(slot))

        return JigEligibility(slots=tuple(results))

    @staticmethod
    def _evaluate_slot(slot: ResolvedSlot) -> SlotEligibility:

        if (slot.status is SlotTopologyStatus.READY):
            return SlotEligibility(slot_number=slot.slot_number,
                status=(SlotEligibilityStatus.ELIGIBLE),
                topology=slot,
                reason=(
                    f"Slot {slot.slot_number} "
                    f"is ready on {slot.com_port}."
                ),
            )

        if (slot.status is SlotTopologyStatus.UNBOUND):
            return SlotEligibility(slot_number=slot.slot_number,
                status=(SlotEligibilityStatus.BLOCKED_UNBOUND),
                topology=slot,
                reason=(
                    f"Slot {slot.slot_number} "
                    "has no commissioned "
                    "serial binding."
                ),
            )

        if (slot.status is SlotTopologyStatus.MISSING):
            return SlotEligibility(slot_number=slot.slot_number,
                status=(SlotEligibilityStatus.BLOCKED_MISSING),
                topology=slot,
                reason=(
                    f"Slot {slot.slot_number} "
                    "serial interface is missing."
                ),
            )

        if (slot.status is SlotTopologyStatus.MOVED):
            return SlotEligibility(slot_number=slot.slot_number,
                status=(SlotEligibilityStatus.BLOCKED_MOVED),
                topology=slot,
                reason=(
                    f"Slot {slot.slot_number} "
                    "serial interface has moved "
                    "from its commissioned "
                    "USB location."
                ),
            )

        raise ValueError(
            f"Unsupported topology state "
            f"{slot.status}"
        )

@dataclass(frozen=True, slots=True)
class SlotOperationAvailability:
    slot_number: int
    available: bool
    reason: str




