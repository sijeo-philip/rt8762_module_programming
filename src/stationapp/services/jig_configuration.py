"""Supervisor-controlled COM-port-to-slot binding (session setup).

Bindings use stable USB identities, never COM numbers. Every change is
audited and, while a session is open, explicitly adopted into the
session's confirmed configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict

from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.unit_of_work import UnitOfWork
from stationapp.domain.audit import AuditRecord
from stationapp.drivers.serial.discovery import discover_stable_usb_ports
from stationapp.drivers.serial.types import DiscoveredSerialPort
from stationapp.services.access import StationAccessService
from stationapp.services.slot_binding import SlotBindingConfiguration, SlotBindingService

logger = logging.getLogger(__name__)


class JigConfigurationService:
    def __init__(self, *, station_id: str, binding_service: SlotBindingService, access: StationAccessService, session_factory: sessionmaker[Session], discover: Callable[[], tuple[DiscoveredSerialPort, ...]] = discover_stable_usb_ports) -> None:
        self._station_id = station_id
        self._bindings = binding_service
        self._access = access
        self._session_factory = session_factory
        self._discover = discover

    def load(self) -> SlotBindingConfiguration:
        return self._bindings.load()

    def detected_ports(self) -> tuple[DiscoveredSerialPort, ...]:
        return self._discover()

    def bind(self, *, slot_number: int, port: DiscoveredSerialPort, reason: str) -> SlotBindingConfiguration:
        supervisor = self._access.ensure_configuration_change_allowed()
        reason = self._reason(reason)
        previous = self._bindings.load().get(slot_number)
        result = self._bindings.bind(slot_number=slot_number, port=port, replace=True)
        self._audit(supervisor.user_id, slot_number, "SLOT_BOUND", reason, previous, result.get(slot_number))
        logger.info("Slot %d bound to %s (%s) by %s", slot_number, port.stable_key, port.device, supervisor.user_id)
        self._adopt(f"Slot {slot_number} rebound: {reason}")
        return result

    def unbind(self, *, slot_number: int, reason: str) -> SlotBindingConfiguration:
        supervisor = self._access.ensure_configuration_change_allowed()
        reason = self._reason(reason)
        previous = self._bindings.load().get(slot_number)
        if previous is None:
            return self._bindings.load()
        result = self._bindings.unbind(slot_number)
        self._audit(supervisor.user_id, slot_number, "SLOT_UNBOUND", reason, previous, None)
        self._adopt(f"Slot {slot_number} unbound: {reason}")
        return result

    def _adopt(self, reason: str) -> None:
        if self._access.session is not None:
            self._access.reconfirm_configuration(reason)

    def _audit(self, actor_id: str, slot_number: int, action: str, reason: str, previous, new) -> None:
        with UnitOfWork(self._session_factory) as uow:
            uow.audit.append(
                AuditRecord(
                    user_id=actor_id,
                    station_id=self._station_id,
                    record_type="JIG_SLOT_BINDING",
                    record_id=f"SLOT-{slot_number}",
                    action=action,
                    reason=reason,
                    previous_value=asdict(previous) if previous is not None else None,
                    new_value=asdict(new) if new is not None else None,
                )
            )
            uow.commit()

    @staticmethod
    def _reason(reason: str) -> str:
        if not reason.strip():
            raise ValueError("A reason is required")
        return reason.strip()
