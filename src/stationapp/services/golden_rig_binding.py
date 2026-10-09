
"""Supervisor-controlled Golden Rig configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from stationapp.domain.audit import AuditRecord
from stationapp.domain.golden_rig_binding import (
    GoldenRigAuthorizationError,
    GoldenRigBinding,
    GoldenRigNotBoundError,
)


class StationRole(str, Enum):
    OPERATOR = "OPERATOR"
    SUPERVISOR = "SUPERVISOR"


class BindingRepository(Protocol):
    def load(self) -> GoldenRigBinding | None:
        ...

    def save(self, binding: GoldenRigBinding) -> None:
        ...


class GoldenRigBindingService:
    """Manages a station's approved Golden Rig binding."""

    def __init__(self, station_id: str, repository: BindingRepository) -> None:
        if not station_id.strip():
            raise ValueError("station_id is required")

        self._station_id = station_id
        self._repository = repository

    def get_binding(self) -> GoldenRigBinding | None:
        return self._repository.load()

    def require_binding(self) -> GoldenRigBinding:
        binding = self._repository.load()

        if binding is None:
            raise GoldenRigNotBoundError("Supervisor must configure a Golden Rig")
        binding.require_station(self._station_id)
        return binding

    def approve(self, *, actor_id: str, actor_role: StationRole, rig_id: str, host: str, port: int, certificate_sha256: str, reason: str) -> AuditRecord:
        self._require_supervisor(actor_role)
        if not actor_id.strip():
            raise ValueError("actor_id is required")
        if not reason.strip():
            raise ValueError("Approval reason is required")
        previous = self._repository.load()

        if (previous is not None and previous.station_id != self._station_id):
            raise GoldenRigAuthorizationError("Existing binding belongs to another station")

        binding = GoldenRigBinding(station_id=self._station_id, rig_id=rig_id, host=host, port=port, certificate_sha256=certificate_sha256, approved_by=actor_id, approved_at=datetime.now(timezone.utc))
        audit = AuditRecord(user_id=actor_id, station_id=self._station_id, record_type="GOLDEN_RIG_BINDING", record_id=binding.rig_id,
            action=("GOLDEN_RIG_REBOUND" if previous is not None else "GOLDEN_RIG_BOUND"),
            reason=reason,
            previous_value=(self._audit_values(previous) if previous is not None else None),
            new_value=self._audit_values(binding),
        )
        self._repository.save(binding)
        return audit

    def disable(self, *, actor_id: str, actor_role: StationRole, reason: str) -> AuditRecord:
        self._require_supervisor(actor_role)
        if not actor_id.strip() or not reason.strip():
            raise ValueError("Supervisor identity and reason are required")
        previous = self.require_binding()
        binding = GoldenRigBinding(station_id=previous.station_id,
            rig_id=previous.rig_id,
            host=previous.host,
            port=previous.port,
            certificate_sha256=previous.certificate_sha256,
            approved_by=previous.approved_by,
            approved_at=previous.approved_at,
            enabled=False,
        )

        audit = AuditRecord(
            user_id=actor_id,
            station_id=self._station_id,
            record_type="GOLDEN_RIG_BINDING",
            record_id=binding.rig_id,
            action="GOLDEN_RIG_DISABLED",
            reason=reason,
            previous_value=self._audit_values(previous),
            new_value=self._audit_values(binding),
        )

        self._repository.save(binding)

        return audit

    @staticmethod
    def _require_supervisor( role: StationRole) -> None:
        if role is not StationRole.SUPERVISOR:
            raise GoldenRigAuthorizationError("Supervisor authorization required")

    @staticmethod
    def _audit_values(binding: GoldenRigBinding) -> dict[str, object]:
        return {
            "station_id": binding.station_id,
            "rig_id": binding.rig_id,
            "host": binding.host,
            "port": binding.port,
            "certificate_sha256": (
                binding.certificate_sha256
            ),
            "enabled": binding.enabled,
        }


    

