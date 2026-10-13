"""Supervisor-controlled production session.

A session records the jig configuration and Golden Rig binding the
Supervisor confirmed when starting production. Batches may run only
while the live station configuration still matches that confirmation;
any change must be made, and re-confirmed, by an authorized Supervisor.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class SessionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class StationConfiguration:
    """Configuration the Supervisor confirms at session start."""

    jig_id: str
    jig_positions: int
    slot_bindings: tuple[tuple[int, str], ...]
    golden_rig_id: str | None
    golden_rig_fingerprint: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "slot_bindings", tuple(sorted(self.slot_bindings)))

    @property
    def bound_slots(self) -> tuple[int, ...]:
        return tuple(slot for slot, _key in self.slot_bindings)

    def differences(self, current: "StationConfiguration") -> tuple[str, ...]:
        problems = []
        if current.jig_id != self.jig_id or current.jig_positions != self.jig_positions:
            problems.append(f"Jig changed from {self.jig_id} to {current.jig_id}")
        if current.slot_bindings != self.slot_bindings:
            confirmed = dict(self.slot_bindings)
            live = dict(current.slot_bindings)
            changed = sorted(
                slot
                for slot in set(confirmed) | set(live)
                if confirmed.get(slot) != live.get(slot)
            )
            problems.append("COM-port bindings changed for slot(s) " + ", ".join(map(str, changed)))
        if (current.golden_rig_id, current.golden_rig_fingerprint) != (self.golden_rig_id, self.golden_rig_fingerprint):
            problems.append("Golden Rig binding changed")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "jig_id": self.jig_id,
            "jig_positions": self.jig_positions,
            "slot_bindings": [[slot, key] for slot, key in self.slot_bindings],
            "golden_rig_id": self.golden_rig_id,
            "golden_rig_fingerprint": self.golden_rig_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StationConfiguration":
        return cls(
            jig_id=data["jig_id"],
            jig_positions=int(data["jig_positions"]),
            slot_bindings=tuple((int(slot), str(key)) for slot, key in data["slot_bindings"]),
            golden_rig_id=data.get("golden_rig_id"),
            golden_rig_fingerprint=data.get("golden_rig_fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class ProductionSession:
    station_id: str
    supervisor_id: str
    configuration: StationConfiguration
    session_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: SessionStatus = SessionStatus.OPEN
    closed_at: datetime | None = None
    closed_by: str | None = None
    close_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.station_id.strip() or not self.supervisor_id.strip():
            raise ValueError("station_id and supervisor_id are required")
        if not self.configuration.slot_bindings:
            raise ValueError("A session requires at least one bound jig slot")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")

    @property
    def is_open(self) -> bool:
        return self.status is SessionStatus.OPEN

    def reconfirmed(self, configuration: StationConfiguration) -> "ProductionSession":
        if not self.is_open:
            raise ValueError("A closed session cannot be changed")
        return replace(self, configuration=configuration)

    def closed(self, *, by: str, reason: str) -> "ProductionSession":
        if not self.is_open:
            raise ValueError("Session is already closed")
        if not reason.strip():
            raise ValueError("A close reason is required")
        return replace(
            self,
            status=SessionStatus.CLOSED,
            closed_at=datetime.now(timezone.utc),
            closed_by=by,
            close_reason=reason.strip(),
        )
