"""Immutable runtime station operation log records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class OperationLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class OperationLog:
    """One immutable station runtime activity record."""

    station_id: str
    level: OperationLevel
    category: str
    event_type: str
    message: str

    operation_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    batch_id: str | None = None
    slot_number: int | None = None
    correlation_id: str | None = None
    user_id: str | None = None
    detail: dict[str, Any] | None = None

    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(
            timezone.utc
        )
    )

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("operation_id cannot be empty")

        if not self.station_id.strip():
            raise ValueError("station_id cannot be empty")

        if not self.category.strip():
            raise ValueError("category cannot be empty")

        if not self.event_type.strip():
            raise ValueError("event_type cannot be empty")

        if not self.message.strip():
            raise ValueError("message cannot be empty")

        if (self.slot_number is not None and self.slot_number <= 0):
            raise ValueError("slot_number must be positive")

        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
