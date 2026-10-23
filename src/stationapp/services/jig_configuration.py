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
from stationapp.services.slot_autobind import DEFAULT_REFERENCE_PREFIX, AutoBindPlan, MacReader, read_reference_modules
from stationapp.services.slot_binding import SlotBinding, SlotBindingConfiguration, SlotBindingService

logger = logging.getLogger(__name__)


class JigConfigurationService:
    def __init__(self, *, station_id: str, binding_service: SlotBindingService, access: StationAccessService, session_factory: sessionmaker[Session], discover: Callable[[], tuple[DiscoveredSerialPort, ...]] = discover_stable_usb_ports, mac_reader: MacReader | None = None, reference_prefix: str = DEFAULT_REFERENCE_PREFIX) -> None:
        self._station_id = station_id
        self._bindings = binding_service
        self._access = access
        self._session_factory = session_factory
        self._discover = discover
        # Reads one port's MAC (MPCLI -I -r) for binding by reference modules.
        self._mac_reader = mac_reader
        self._reference_prefix = reference_prefix

    @property
    def reference_prefix(self) -> str:
        return self._reference_prefix

    @property
    def reference_binding_available(self) -> bool:
        return self._mac_reader is not None

    def scan_reference_modules(self) -> AutoBindPlan:
        """Read the MAC on every detected port; propose slot bindings.

        Supervisor only. Nothing is saved until apply_reference_plan().
        """
        self._access.require_supervisor()
        if self._mac_reader is None:
            raise ValueError("Binding by reference modules is not available on this station")
        configuration = self._bindings.load()
        ports = self._discover()
        return read_reference_modules(
            [(port.device, port.stable_key) for port in ports],
            self._mac_reader,
            jig_positions=configuration.jig_positions,
            prefix=self._reference_prefix,
        )

    def apply_reference_plan(self, plan: AutoBindPlan, *, reason: str) -> SlotBindingConfiguration:
        """Save every slot the reference scan identified, in one change.

        Slots it did not identify keep their binding, unless their USB
        interface now belongs to an identified slot (then they are unbound).
        """
        supervisor = self._access.ensure_configuration_change_allowed()
        reason = self._reason(reason)
        if not plan.assignments:
            raise ValueError("The scan identified no slot; nothing to bind")
        detected = {port.stable_key: port for port in self._discover()}
        new: dict[int, SlotBinding] = {}
        for slot, reading in plan.assignments.items():
            port = detected.get(reading.stable_key or "")
            if port is None:
                raise ValueError(f"{reading.device} ({reading.stable_key}) is no longer detected; scan again")
            new[slot] = SlotBinding.from_port(slot, port)

        current = self._bindings.load()
        assigned_keys = {binding.stable_key for binding in new.values()}
        kept = [
            binding for binding in current.bindings
            if binding.slot_number not in new and binding.stable_key not in assigned_keys
        ]
        result = SlotBindingConfiguration(
            station_id=current.station_id,
            jig_id=current.jig_id,
            jig_positions=current.jig_positions,
            bindings=tuple(sorted([*kept, *new.values()], key=lambda item: item.slot_number)),
        )
        self._bindings.save(result)

        text = f"{reason} (reference modules {self._reference_prefix}:NN)"
        for binding in current.bindings:
            if binding.slot_number not in new and binding.stable_key in assigned_keys:
                moved_to = next(slot for slot, item in new.items() if item.stable_key == binding.stable_key)
                self._audit(supervisor.user_id, binding.slot_number, "SLOT_UNBOUND", f"{text}; its interface is slot {moved_to}", binding, None)
        for slot, binding in sorted(new.items()):
            previous = current.get(slot)
            if previous is None or previous.stable_key != binding.stable_key:
                self._audit(supervisor.user_id, slot, "SLOT_BOUND", text, previous, binding)
        logger.info("Slots bound from reference modules by %s: %s", supervisor.user_id,
                    ", ".join(f"{slot}={plan.assignments[slot].device}" for slot in sorted(new)))
        self._adopt(f"Slots bound from reference modules: {reason}")
        return result

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
