
"""Immutable manufacturing event vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class EventType(str, Enum):
    ALLOCATION_IMPORTED = "ALLOCATION_IMPORTED"

    BATCH_CREATED = "BATCH_CREATED"
    MODULE_BOUND = "MODULE_BOUND"
    PORT_BOUND = "PORT_BOUND"

    STOCK_MAC_RESERVED = "STOCK_MAC_RESERVED"
    STOCK_PROGRAMMING_STARTED = "STOCK_PROGRAMMING_STARTED"
    STOCK_PROGRAMMED = "STOCK_PROGRAMMED"
    STOCK_PROGRAMMING_UNCERTAIN = "STOCK_PROGRAMMING_UNCERTAIN"
    STOCK_RF_CONFIRMED = "STOCK_RF_CONFIRMED"

    PRICOL_MAC_RESERVED = "PRICOL_MAC_RESERVED"
    PRICOL_PROGRAMMING_STARTED = "PRICOL_PROGRAMMING_STARTED"
    PRICOL_PROGRAMMED = "PRICOL_PROGRAMMED"
    PRICOL_PROGRAMMING_UNCERTAIN = "PRICOL_PROGRAMMING_UNCERTAIN"
    PRICOL_MAC_READBACK_CONFIRMED = "PRICOL_MAC_READBACK_CONFIRMED"

    PRICOLAPP_RESULT = "PRICOLAPP_RESULT"
    PRICOLDFU_RESULT = "PRICOLDFU_RESULT"

    HOLD_PLACED = "HOLD_PLACED"
    BATCH_COMMITTED = "BATCH_COMMITTED"
    UPLOAD_QUEUED = "UPLOAD_QUEUED"
    UPLOAD_CONFIRMED = "UPLOAD_CONFIRMED"


@dataclass(frozen=True, slots=True)
class ManufacturingEvent:
    event_type: EventType
    batch_id: str
    station_id: str
    jig_id: str
    payload: dict[str, Any]

    event_id: str = field(default_factory=lambda: str(uuid4()))
    slot_number: int | None = None
    module_qr: str | None = None
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if not self.batch_id.strip():
            raise ValueError("batch_id cannot be empty")

        if not self.station_id.strip():
            raise ValueError("station_id cannot be empty")

        if not self.jig_id.strip():
            raise ValueError("jig_id cannot be empty")

        if self.slot_number is not None and self.slot_number <= 0:
            raise ValueError("slot_number must be positive")

        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
