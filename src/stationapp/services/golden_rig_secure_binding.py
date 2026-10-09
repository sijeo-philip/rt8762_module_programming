
"""Transactional, supervisor-controlled Golden Rig approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from stationapp.domain.audit import AuditRecord

from stationapp.domain.golden_rig_binding import (
    GoldenRigAuthorizationError,
    GoldenRigBinding,
    GoldenRigNotBoundError,
)


@dataclass(frozen=True, slots=True)
class AuthenticatedSupervisor:
    """Identity produced by the trusted authentication boundary."""

    user_id: str


class BindingStore(Protocol):
    def load(self, station_id: str) -> GoldenRigBinding | None:
        ...

    def save_with_audit(self, binding: GoldenRigBinding, audit: AuditRecord) -> None:
        ...


class SecureGoldenRigBindingService:

    def __init__(self, station_id: str, repository: BindingStore) -> None:
        if not station_id.strip():
            raise ValueError("station_id required")
        self._station_id = station_id
        self._repository = repository

    def require_binding(self) -> GoldenRigBinding:
        binding = self._repository.load(self._station_id)
        if binding is None:
            raise GoldenRigNotBoundError("Golden Rig not approved for station")
        binding.require_station(self._station_id)
        return binding

    def approve(self, *, supervisor: AuthenticatedSupervisor, rig_id: str, host: str, port: int, certificate_sha256: str, reason: str) -> AuditRecord:
        self._require_supervisor(supervisor)
        if not reason.strip():
            raise ValueError("Approval reason required")
        previous = self._repository.load(self._station_id)

        binding = GoldenRigBinding(station_id=self._station_id,
            rig_id=rig_id,
            host=host,
            port=port,
            certificate_sha256=certificate_sha256,
            approved_by=supervisor.user_id,
            approved_at=datetime.now(timezone.utc),
            enabled=True,
        )

        audit = AuditRecord(
            user_id=supervisor.user_id,
            station_id=self._station_id,
            record_type="GOLDEN_RIG_BINDING",
            record_id=rig_id,
            action=("GOLDEN_RIG_REBOUND" if previous is not None else "GOLDEN_RIG_BOUND" ),
            reason=reason,
            previous_value=(self._snapshot(previous) if previous is not None else None),
            new_value=self._snapshot(binding),
        )

        self._repository.save_with_audit(binding, audit)
        return audit

    def disable(self, *, supervisor: AuthenticatedSupervisor, reason: str) -> AuditRecord:
        self._require_supervisor(supervisor)
        if not reason.strip():
            raise ValueError("Disable reason required")
        previous = self.require_binding()
        disabled = GoldenRigBinding(
            station_id=previous.station_id,
            rig_id=previous.rig_id,
            host=previous.host,
            port=previous.port,
            certificate_sha256=previous.certificate_sha256,
            approved_by=previous.approved_by,
            approved_at=previous.approved_at,
            enabled=False,
        )

        audit = AuditRecord(
            user_id=supervisor.user_id,
            station_id=self._station_id,
            record_type="GOLDEN_RIG_BINDING",
            record_id=previous.rig_id,
            action="GOLDEN_RIG_DISABLED",
            reason=reason,
            previous_value=self._snapshot(previous),
            new_value=self._snapshot(disabled),
        )

        self._repository.save_with_audit(disabled, audit)
        return audit

    @staticmethod
    def _require_supervisor(supervisor: AuthenticatedSupervisor) -> None:
        if (not isinstance(supervisor, AuthenticatedSupervisor) or not isinstance(supervisor.user_id, str) or not supervisor.user_id.strip()):
            raise GoldenRigAuthorizationError("Authenticated supervisor required")

    @staticmethod
    def _snapshot(binding: GoldenRigBinding) -> dict[str, object]:
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

    
