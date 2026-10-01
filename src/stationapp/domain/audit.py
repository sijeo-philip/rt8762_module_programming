"""Immutable audit record used for controlled station changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable controlled-change audit entry.

    Audit records answer:

        who changed what,
        from which value,
        to which value,
        why,
        where,
        and when.
    """

    user_id: str
    station_id: str
    record_type: str
    record_id: str
    action: str
    reason: str

    previous_value: dict[str, Any] | None = None
    new_value: dict[str, Any] | None = None

    audit_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if not self.audit_id.strip():
            raise ValueError(
                "audit_id cannot be empty"
            )

        if not self.user_id.strip():
            raise ValueError(
                "user_id cannot be empty"
            )

        if not self.station_id.strip():
            raise ValueError(
                "station_id cannot be empty"
            )

        if not self.record_type.strip():
            raise ValueError(
                "record_type cannot be empty"
            )

        if not self.record_id.strip():
            raise ValueError(
                "record_id cannot be empty"
            )

        if not self.action.strip():
            raise ValueError(
                "action cannot be empty"
            )

        if not self.reason.strip():
            raise ValueError(
                "reason cannot be empty"
            )

        if self.created_at.tzinfo is None:
            raise ValueError(
                "created_at must be timezone-aware"
            )