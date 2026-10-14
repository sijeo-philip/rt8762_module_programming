"""Supervisor-controlled production settings.

The Operator never chooses the programming mode, firmware or which jig
slots are in service; the Supervisor sets them here. Batches use the
current settings, and every change is audited with old and new values.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.models import StationSettingModel
from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.domain.batch import ProgrammingMode

logger = logging.getLogger(__name__)

KEY = "production_settings"


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    mode: ProgrammingMode
    stock_firmware: Path | None
    pricol_firmware: Path | None
    enabled_slots: tuple[int, ...]
    lot: str | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "stock_firmware": str(self.stock_firmware) if self.stock_firmware else None,
            "pricol_firmware": str(self.pricol_firmware) if self.pricol_firmware else None,
            "enabled_slots": list(self.enabled_slots),
            "lot": self.lot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProductionSettings":
        return cls(
            mode=ProgrammingMode(data["mode"]),
            stock_firmware=Path(data["stock_firmware"]) if data.get("stock_firmware") else None,
            pricol_firmware=Path(data["pricol_firmware"]) if data.get("pricol_firmware") else None,
            enabled_slots=tuple(int(slot) for slot in data.get("enabled_slots", [])),
            lot=data.get("lot") or None,
        )


class ProductionSettingsService:
    def __init__(self, *, session_factory: sessionmaker[Session], station_id: str, jig_positions: int, defaults: ProductionSettings, access=None) -> None:
        self._session_factory = session_factory
        self._station_id = station_id
        self._jig_positions = jig_positions
        self._defaults = defaults
        self._access = access
        self._listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._cached: ProductionSettings | None = None

    @property
    def jig_positions(self) -> int:
        return self._jig_positions

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def current(self) -> ProductionSettings:
        with self._lock:
            if self._cached is None:
                with self._session_factory() as session:
                    row = session.get(StationSettingModel, KEY)
                    self._cached = ProductionSettings.from_dict(row.value) if row is not None else self._defaults
            return self._cached

    def update(self, settings: ProductionSettings, *, reason: str) -> ProductionSettings:
        actor = self._access.ensure_configuration_change_allowed() if self._access is not None else None
        if not reason.strip():
            raise ValueError("A reason is required")
        self._validate(settings)
        previous = self.current()

        with UnitOfWork(self._session_factory) as uow:
            row = uow.session.get(StationSettingModel, KEY)
            if row is None:
                row = StationSettingModel(key=KEY, value={})
                uow.session.add(row)
            row.value = settings.to_dict()
            row.updated_at = datetime.now(timezone.utc)
            row.updated_by = actor.user_id if actor else None
            uow.audit.append(AuditRecord(
                user_id=actor.user_id if actor else "SYSTEM",
                station_id=self._station_id,
                record_type="PRODUCTION_SETTINGS",
                record_id=KEY,
                action="PRODUCTION_SETTINGS_CHANGED",
                reason=reason.strip(),
                previous_value=previous.to_dict(),
                new_value=settings.to_dict(),
            ))
            uow.commit()

        with self._lock:
            self._cached = settings
        logger.info("Production settings changed by %s: %s", actor.user_id if actor else "SYSTEM", settings.to_dict())
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                logger.exception("Settings listener failed")
        return settings

    def _validate(self, settings: ProductionSettings) -> None:
        slots = settings.enabled_slots
        if not slots:
            raise ValueError("Enable at least one jig slot")
        if len(set(slots)) != len(slots) or any(not 1 <= slot <= self._jig_positions for slot in slots):
            raise ValueError(f"Enabled slots must be between 1 and {self._jig_positions}")
        if settings.stock_firmware is None:
            raise ValueError("Select the Stock firmware image")
        if settings.mode is ProgrammingMode.STOCK_AND_PRICOL and settings.pricol_firmware is None:
            raise ValueError("Select the Pricol firmware image for Stock + Pricol mode")
