"""Station READY gate: jig detection, configuration validation, MAC cache.

Implements workflow steps 3-7. A batch may start only when every
mandatory check passes for the selected programming mode and the slots
the operator has loaded. The production controller calls this gate
again at START, so a stale enabled button cannot bypass it.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from stationapp.config import Settings
from stationapp.domain.batch import ProgrammingMode
from stationapp.domain.mac import MacPurpose
from stationapp.services.health import HealthCheck, Severity
from stationapp.services.mac_cache import MacCacheService
from stationapp.services.production import SlotPort
from stationapp.services.serial_topology import SerialTopology
from stationapp.services.slot_eligibility import JigEligibility, SlotEligibilityService

logger = logging.getLogger(__name__)

GoldenRigCheck = Callable[[], HealthCheck]


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: tuple[HealthCheck, ...]
    ports: dict[int, SlotPort] = field(default_factory=dict)
    eligibility: JigEligibility | None = None

    @property
    def ready(self) -> bool:
        return not any(check.is_blocking for check in self.checks)

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {check.detail}"
            for check in self.checks
            if check.is_blocking
        )

    @property
    def available_slots(self) -> tuple[int, ...]:
        return tuple(sorted(self.ports))


class StationReadinessService:
    """Evaluate whether the station may start a batch."""

    def __init__(self, *, settings: Settings, topology_service, eligibility_service: SlotEligibilityService, mac_cache: MacCacheService | None, golden_rig_check: GoldenRigCheck, simulation: bool = False, functional_test_available: bool = False, session_check: Callable[[], HealthCheck] | None = None, sync_check: Callable[[], HealthCheck] | None = None) -> None:
        self._settings = settings
        self._topology = topology_service
        self._eligibility = eligibility_service
        self._mac_cache = mac_cache
        self._golden_rig_check = golden_rig_check
        self._simulation = simulation
        self._functional_test_available = functional_test_available or simulation
        self._session_check = session_check
        self._sync_check = sync_check

    def evaluate(self, mode: ProgrammingMode, occupied: tuple[int, ...] | None = None, *, top_up_macs: bool = False) -> ReadinessReport:
        checks: list[HealthCheck] = []

        if self._session_check is not None:
            checks.append(self._session_check())

        ports, eligibility, jig_checks = self._jig(occupied)
        checks.extend(jig_checks)
        checks.extend(self._tools(mode))
        checks.extend(self._mac_checks(mode, occupied, top_up_macs))
        checks.append(self._golden_rig())
        checks.append(self._lan())
        if self._sync_check is not None:
            try:
                checks.append(self._sync_check())
            except Exception as exc:  # noqa: BLE001 - sync never blocks production
                logger.exception("Sync status check failed")
                checks.append(HealthCheck("Server sync", Severity.WARN, f"Sync status unavailable: {exc}"))

        return ReadinessReport(checks=tuple(checks), ports=ports, eligibility=eligibility)

    def gate(self, mode: ProgrammingMode, occupied: tuple[int, ...]) -> tuple[str, ...]:
        """Controller-facing gate: blocking problems for this START."""
        return self.evaluate(mode, occupied).problems

    # ------------------------------------------------------------------

    def _jig(self, occupied: tuple[int, ...] | None) -> tuple[dict[int, SlotPort], JigEligibility | None, list[HealthCheck]]:
        positions = self._settings.jig_positions

        if self._simulation:
            ports = {
                number: SlotPort(number, f"SIM:{self._settings.jig_id}:SLOT{number}", f"COM{100 + number}")
                for number in range(1, positions + 1)
            }
            return ports, None, [HealthCheck("Jig", Severity.WARN, f"SIMULATED {positions}-slot jig - no hardware is used")]

        try:
            topology: SerialTopology = self._topology.refresh()
            eligibility = self._eligibility.evaluate(topology)
        except Exception as exc:  # noqa: BLE001 - shown to the supervisor
            logger.exception("Jig detection failed")
            return {}, None, [HealthCheck("Jig", Severity.FAIL, f"Jig detection failed: {exc}")]

        ports: dict[int, SlotPort] = {}
        for item in eligibility.eligible_slots:
            resolved = topology.resolve_slot(item.slot_number)
            assert item.com_port is not None and resolved.stable_key is not None
            ports[item.slot_number] = SlotPort(item.slot_number, resolved.stable_key, item.com_port)

        checks = [
            HealthCheck(
                "Jig",
                Severity.OK if ports else Severity.FAIL,
                f"{self._settings.jig_id}: {len(ports)}/{positions} slots bound and detected",
            )
        ]

        if occupied is not None:
            blocked = [number for number in occupied if number not in ports]
            if blocked:
                reasons = "; ".join(eligibility.get(number).reason for number in blocked)
                checks.append(HealthCheck("Loaded slots", Severity.FAIL, reasons))
            else:
                checks.append(HealthCheck("Loaded slots", Severity.OK, "Slots " + ", ".join(map(str, occupied)) + " ready"))

        return ports, eligibility, checks

    def _tools(self, mode: ProgrammingMode) -> list[HealthCheck]:
        if self._simulation:
            return [HealthCheck("Programming tools", Severity.WARN, "SIMULATED MPCLI, Golden Rig and functional test")]

        settings = self._settings
        checks = [
            _file_check("MPCLI", settings.mpcli_path),
            _file_check("Stock firmware", settings.stock_firmware_path),
        ]

        if mode is ProgrammingMode.STOCK_AND_PRICOL:
            checks.append(_file_check("Pricol firmware", settings.pricol_firmware_path))
            if not self._functional_test_available:
                checks.append(HealthCheck(
                    "Functional test",
                    Severity.FAIL,
                    "No Pricol functional tester configured; use Stock only",
                ))

        if not settings.mpcli_product_id or settings.mpcli_secret_key is None:
            checks.append(HealthCheck("MPCLI credentials", Severity.FAIL, "Set MPCLI_PRODUCT_ID and MPCLI_SECRET_KEY in .env"))
        else:
            checks.append(HealthCheck("MPCLI credentials", Severity.OK, "configured"))

        return checks

    def _mac_checks(self, mode: ProgrammingMode, occupied: tuple[int, ...] | None, top_up: bool) -> list[HealthCheck]:
        if self._mac_cache is None:
            return [HealthCheck("MAC cache", Severity.FAIL, "Local MAC cache unavailable")]

        purposes = [MacPurpose.STOCK_RF_TEST]
        if mode is ProgrammingMode.STOCK_AND_PRICOL:
            purposes.append(MacPurpose.PRICOL_PRODUCTION)

        needed = len(occupied) if occupied else self._settings.jig_positions
        checks = []
        for purpose in purposes:
            label = "Stock MACs" if purpose is MacPurpose.STOCK_RF_TEST else "Pricol MACs"
            try:
                if top_up:
                    status = self._mac_cache.ensure(purpose)
                    available, message = status.available, status.message
                else:
                    available = self._mac_cache.available(purpose)
                    message = f"{available} authorized MACs cached"
            except Exception as exc:  # noqa: BLE001
                logger.exception("MAC cache check failed")
                checks.append(HealthCheck(label, Severity.FAIL, f"MAC cache check failed: {exc}"))
                continue

            if available < needed:
                checks.append(HealthCheck(label, Severity.FAIL, f"{message}; {needed} required for this load"))
            elif available < self._settings.mac_cache_threshold:
                checks.append(HealthCheck(label, Severity.WARN, f"{message}; below threshold {self._settings.mac_cache_threshold}"))
            else:
                checks.append(HealthCheck(label, Severity.OK, message))
        return checks

    def _golden_rig(self) -> HealthCheck:
        try:
            return self._golden_rig_check()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Golden Rig check failed")
            return HealthCheck("Golden Rig", Severity.FAIL, f"Golden Rig check failed: {exc}")

    def _lan(self) -> HealthCheck:
        if self._settings.is_stage_two:
            return HealthCheck("LAN server", Severity.OK, str(self._settings.lan_server_url))
        return HealthCheck("LAN server", Severity.WARN, "Not configured - offline production; uploads stay queued")


def _file_check(name: str, path: Path | None) -> HealthCheck:
    if path is None:
        return HealthCheck(name, Severity.FAIL, "not configured")
    if not Path(path).is_file():
        return HealthCheck(name, Severity.FAIL, f"not found: {path}")
    return HealthCheck(name, Severity.OK, str(path))


def approved_golden_rig_check(binding_service, ca_certificate: Path, *, probe_timeout: float = 1.0) -> GoldenRigCheck:
    """Approved binding exists, is enabled, and the rig is reachable.

    Rig identity itself is proven by certificate pinning on every RF
    request; this check only stops a batch from starting when the
    approved rig is clearly missing.
    """

    def check() -> HealthCheck:
        binding = binding_service.require_binding()

        if not Path(ca_certificate).is_file():
            return HealthCheck("Golden Rig", Severity.FAIL, f"CA certificate missing: {ca_certificate}")

        try:
            with socket.create_connection((binding.host, binding.port), timeout=probe_timeout):
                pass
        except OSError as exc:
            return HealthCheck(
                "Golden Rig",
                Severity.FAIL,
                f"Approved rig {binding.rig_id} unreachable at {binding.host}:{binding.port} ({exc}). Supervisor intervention required.",
            )

        return HealthCheck("Golden Rig", Severity.OK, f"{binding.rig_id} at {binding.host}:{binding.port}, approved by {binding.approved_by}")

    return check
