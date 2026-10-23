""" Application bootstrap.

Wires the pieces that much exis before anything else runs, in the right order:
1. settings     -- Validated configuration
2. Logging     -- Configured uisng those settings
3. database    -- Migrated to the current schema, uncertain MACs quarantined
4. services    -- Real hardware, or simulators when SIMULATION_MODE is set
5. runtime context -- an immutable object handed to the UI and services

Doing this in one places (rather than scattering get_settings() and setup_logging() calls through
the UI) means there is exactly one startup path, which is the one that gets tested and later
gets the health checks.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from stationapp.config import Settings, get_settings
from stationapp.logging_setup import setup_logging

from stationapp.services.serial_topology import (StationSerialTopologyService, SlotTopologyStatus, SerialTopology)
from stationapp.services.slot_binding import (SlotBindingService)

from stationapp.services.slot_eligibility import (SlotEligibilityService)

from stationapp.services.health import HealthCheck, Severity
from stationapp.services.mac_cache import MacCacheService
from stationapp.services.production import ProductionController
from stationapp.services.readiness import StationReadinessService, approved_golden_rig_check
from stationapp.services.access import StationAccessService, UserAdministrationService
from stationapp.services.golden_rig_configuration import GoldenRigConfigurationService
from stationapp.services.jig_configuration import JigConfigurationService
from stationapp.services.server_sync import SyncScheduler
from stationapp.drivers.qr_scanner import ScanFormat

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class AppContext:
    """ Immutable bundle of everything the app needs at runtime.
    Passed explicitly to the UI and to services, rather than the reaching for
    globals. This is direct antidote to the pattern in Clent_8jig_qr.txt,
    where 'current_user', 'client_socket', and 'mac_enteries' are module-level
    globals mutated from everywhere -- which is why that file has no tests
    and why nothing survives a restart.
    """

    settings: Settings
    started_at: datetime
    serial_topology_service: StationSerialTopologyService
    slot_eligibility_service: SlotEligibilityService

    # Production services. None in UI tests that only exercise topology.
    production: ProductionController | None = None
    readiness: StationReadinessService | None = None
    mac_cache: MacCacheService | None = None
    recovered_macs: int = 0

    # Access control and station configuration. None in UI tests.
    access: StationAccessService | None = None
    users: UserAdministrationService | None = None
    jig_configuration: JigConfigurationService | None = None
    golden_rig_configuration: GoldenRigConfigurationService | None = None
    sync_scheduler: SyncScheduler | None = None
    production_settings: object | None = None       # ProductionSettingsService
    authentication: object | None = None            # ServerAuthenticationService


    @property
    def app_version(self) -> str:
        from stationapp import __version__
        return __version__

    @property
    def station_label(self) -> str:
        return (
        f"{self.settings.station_id} / "
        f"{self.settings.jig_id}"
        )

    @property
    def simulation(self) -> bool:
        return self.settings.simulation_mode

def bootstrap() -> AppContext:
    """ Initialize the application runtime and return its context.

    Raises:
        pydantic.ValidationError: if configuration is missing or invalid.
        Delibrately NOT caught here -- a misconfigured station must fail loudly
        at startup, not run with defaults and corrupt the traceability record.
    """
    settings = get_settings()
    slot_eligibility_service = (SlotEligibilityService())
    app_logger = setup_logging(settings.log_dir, settings.log_level)
    app_logger.info("-"*60)
    app_logger.info(" Station App is %s starting | station=%s jig=%s position=%d",
    _app_version(), settings.station_id, settings.jig_id, settings.jig_positions)
    app_logger.info("Host: %s | Python %s", platform.node(), platform.python_version())
    app_logger.info("Stage 2 (LAN SERVER) %s", "configured" if settings.is_stage_two else
    "not configure -- offline mode")
    if settings.simulation_mode:
        app_logger.warning("SIMULATION MODE: no hardware is used; records are not production evidence")

    binding_service = SlotBindingService(
            binding_file=(settings.serial_binding_file),
            station_id=settings.station_id,
            jig_id=settings.jig_id,
            jig_positions=settings.jig_positions,
        )
    serial_topology_service = (StationSerialTopologyService(binding_service=binding_service))

    session_factory, recovered = _prepare_database(settings)
    access, users, jig_configuration, golden_rig_configuration = _build_access(
        settings, session_factory, binding_service
    )
    lan_client, qr_claims, sync_scheduler = _build_lan(settings, session_factory)
    production_settings = _build_production_settings(settings, session_factory, access)
    production, readiness, mac_cache = _build_production(
        settings, session_factory, serial_topology_service, slot_eligibility_service, access, qr_claims, sync_scheduler, production_settings
    )
    access.set_batch_activity_probe(lambda: production.has_active_batch)
    production.add_commit_listener(sync_scheduler.on_batch_committed)
    if sync_scheduler.service.configured and sync_scheduler.service.threshold_reached():
        # A backlog from an earlier run is uploaded straight away.
        sync_scheduler.request("STARTUP")

    context = AppContext(
            settings=settings,
            started_at=datetime.now(timezone.utc),
            serial_topology_service=serial_topology_service,
            slot_eligibility_service=slot_eligibility_service,
            production=production,
            readiness=readiness,
            mac_cache=mac_cache,
            recovered_macs=recovered,
            access=access,
            users=users,
            jig_configuration=jig_configuration,
            golden_rig_configuration=golden_rig_configuration,
            sync_scheduler=sync_scheduler,
            production_settings=production_settings,
            authentication=access.authentication,
        )
    logger.debug("Bootstrap Complete")
    return context


def _prepare_database(settings: Settings):
    from stationapp.data.database import create_session_factory, create_station_engine
    from stationapp.data.repositories import RecoveryRepository
    from stationapp.data.schema import upgrade_to_head

    upgrade_to_head(settings.database_url)
    engine = create_station_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    # A MAC left in PROGRAMMING means the app stopped mid-write: its
    # outcome is unknown, so it is quarantined, never reused.
    recovered = RecoveryRepository(session_factory).recover_uncertain_programming()
    if recovered:
        logger.warning("Startup recovery quarantined %d MAC(s) left in PROGRAMMING", recovered)

    return session_factory, recovered


def _build_access(settings: Settings, session_factory, binding_service: SlotBindingService):
    """Authentication, sessions, and Supervisor station configuration."""
    from datetime import timedelta

    from stationapp.domain.session import StationConfiguration
    from stationapp.infrastructure.golden_rig.sqlite_binding_store import SqliteGoldenRigBindingStore
    from stationapp.drivers.lan.client import LanServerClient
    from stationapp.services.remote_auth import ServerAuthenticationService
    from stationapp.services.golden_rig_configuration import (
        GoldenRigIdentityProbe,
        live_rig_configuration,
        parse_endpoints,
    )
    from stationapp.services.golden_rig_secure_binding import SecureGoldenRigBindingService

    station_id = settings.station_id
    rig_store = SqliteGoldenRigBindingStore(settings.database_url)

    if settings.simulation_mode:
        from stationapp.services.simulation import SIMULATED_RIG_ID

        def configuration() -> StationConfiguration:
            return StationConfiguration(
                jig_id=settings.jig_id,
                jig_positions=settings.jig_positions,
                slot_bindings=tuple(
                    (number, f"SIM:{settings.jig_id}:SLOT{number}")
                    for number in range(1, settings.jig_positions + 1)
                ),
                golden_rig_id=SIMULATED_RIG_ID,
                golden_rig_fingerprint="SIMULATED",
            )
    else:
        def configuration() -> StationConfiguration:
            bindings = binding_service.load()
            rig_id, fingerprint = live_rig_configuration(rig_store, station_id)
            return StationConfiguration(
                jig_id=settings.jig_id,
                jig_positions=settings.jig_positions,
                slot_bindings=tuple((item.slot_number, item.stable_key) for item in bindings.bindings),
                golden_rig_id=rig_id,
                golden_rig_fingerprint=fingerprint,
            )

    # Logins are verified by the LAN server; users are enrolled there.
    if settings.auth_mode == "test" or (settings.simulation_mode and not settings.lan_server_url):
        from stationapp.services.test_users import TestAuthServer

        auth_client = LanServerClient(base_url="http://test-auth.local", api_key=None, station_id=station_id, transport=TestAuthServer().transport())
        auth_description = "TEST USERS (hardcoded) - not for production"
        test_users = True
        logger.warning("AUTH: hardcoded test users are active - never use on a production station")
    elif settings.lan_server_url:
        auth_client = LanServerClient(
            base_url=settings.lan_server_url,
            api_key=settings.lan_server_api_key,
            station_id=station_id,
            timeout_seconds=settings.lan_server_timeout_seconds,
        )
        auth_description = f"Verified by LAN server {settings.lan_server_url}"
        test_users = False
    else:
        auth_client = None
        auth_description = "LAN server not configured - nobody can log in"
        test_users = False
    authentication = ServerAuthenticationService(
        client=auth_client,
        session_factory=session_factory,
        station_id=station_id,
        jig_id=settings.jig_id,
        mode_description=auth_description,
        test_users=test_users,
    )

    golden_rig_holder: list[GoldenRigConfigurationService] = []

    def verify_rig(confirmed: StationConfiguration) -> tuple[str, ...]:
        # README 1A step 8: the live rig must present the approved identity.
        return golden_rig_holder[0].verify_bound_rig(confirmed)

    access = StationAccessService(
        session_factory=session_factory,
        station_id=station_id,
        authentication=authentication,
        configuration_provider=configuration,
        rig_verifier=None if settings.simulation_mode else verify_rig,
        supervisor_timeout=timedelta(minutes=settings.supervisor_timeout_minutes),
    )

    golden_rig = GoldenRigConfigurationService(
        station_id=station_id,
        store=rig_store,
        binding_service=SecureGoldenRigBindingService(station_id, rig_store),
        probe=GoldenRigIdentityProbe(
            ca_certificate=settings.golden_rig_ca_certificate,
            inventory=settings.golden_rig_inventory.split(","),
        ),
        access=access,
        candidates=parse_endpoints(settings.golden_rig_candidates),
    )
    golden_rig_holder.append(golden_rig)

    # Enrolment happens on the LAN server; no local user administration.
    users = None
    jig = JigConfigurationService(
        station_id=station_id,
        binding_service=binding_service,
        access=access,
        session_factory=session_factory,
        mac_reader=_reference_mac_reader(settings),
        reference_prefix=settings.slot_reference_mac_prefix,
    )

    if access.session is not None:
        logger.info("Production session %s restored; operator must log in again", access.session.session_id)

    return access, users, jig, golden_rig


def _build_lan(settings: Settings, session_factory):
    """LAN server client, line-wide QR claims and threshold-driven sync."""
    from stationapp.drivers.lan.client import LanServerClient
    from stationapp.services.qr_registry import ModuleQrClaimService
    from stationapp.services.server_sync import ServerSyncService

    client = None
    if settings.lan_server_url:
        client = LanServerClient(
            base_url=settings.lan_server_url,
            api_key=settings.lan_server_api_key,
            station_id=settings.station_id,
            timeout_seconds=settings.lan_server_timeout_seconds,
        )
    elif settings.simulation_mode:
        from stationapp.services.simulation import InMemoryLanServer

        simulated = InMemoryLanServer()
        client = LanServerClient(
            base_url="http://simulated-lan-server",
            api_key=None,
            station_id=settings.station_id,
            transport=simulated.transport(),
        )
        logger.warning("SIMULATION: using an in-memory LAN server")

    claims = ModuleQrClaimService(
        client=client,
        jig_id=settings.jig_id,
        claim_timeout_seconds=settings.qr_claim_timeout_seconds,
        enabled=settings.qr_online_claim,
    )
    service = ServerSyncService(
        session_factory=session_factory,
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        client=client,
        threshold_modules=settings.sync_module_threshold,
    )
    scheduler = SyncScheduler(service, retry_seconds=settings.sync_retry_minutes * 60)
    return client, claims, scheduler


def _sync_health(scheduler: SyncScheduler) -> HealthCheck:
    status = scheduler.service.status()
    if not status.configured:
        return HealthCheck("Server sync", Severity.WARN, f"No LAN server; {status.pending_modules} modules waiting to upload")
    if status.qr_conflicts:
        return HealthCheck("Server sync", Severity.WARN, f"{status.qr_conflicts} duplicate QR conflict(s) reported by the LAN server - Supervisor review required")
    if status.threshold_reached:
        detail = f"{status.pending_modules} modules waiting (threshold {status.threshold})"
        if status.last_error:
            detail += f"; last sync failed: {status.last_error}"
        return HealthCheck("Server sync", Severity.WARN, detail)
    return HealthCheck("Server sync", Severity.OK, f"{status.pending_modules}/{status.threshold} modules until next sync")


def _build_production_settings(settings: Settings, session_factory, access: StationAccessService):
    from stationapp.domain.batch import ProgrammingMode
    from stationapp.services.production_settings import ProductionSettings, ProductionSettingsService

    defaults = ProductionSettings(
        mode=ProgrammingMode.STOCK_AND_PRICOL,
        stock_firmware=settings.stock_firmware_path,
        pricol_firmware=settings.pricol_firmware_path,
        enabled_slots=tuple(range(1, settings.jig_positions + 1)),
    )
    return ProductionSettingsService(
        session_factory=session_factory,
        station_id=settings.station_id,
        jig_positions=settings.jig_positions,
        defaults=defaults,
        access=access,
    )


def _build_production(settings: Settings, session_factory, topology_service, eligibility_service, access: StationAccessService, qr_claims, sync_scheduler: SyncScheduler, production_settings):
    from stationapp.data.production_repository import SqlProductionStore

    simulation = settings.simulation_mode

    if simulation:
        from stationapp.services.simulation import (
            SimulatedFunctionalTester,
            SimulatedGoldenRig,
            SimulatedMacAllocationGateway,
            SimulatedProgrammer,
            parse_failures,
        )

        failures = parse_failures(settings.simulation_failures)
        delay = settings.simulation_delay_seconds
        gateway = SimulatedMacAllocationGateway()
        rf_tester = SimulatedGoldenRig(delay_seconds=delay, failures=failures)
        functional = SimulatedFunctionalTester(delay_seconds=delay, failures=failures)

        def programmer_factory(stock: Path | None, pricol: Path | None):
            return SimulatedProgrammer(delay_seconds=delay, failures=failures)

        def golden_rig_check() -> HealthCheck:
            return HealthCheck("Golden Rig", Severity.WARN, "SIMULATED Golden Rig")
    else:
        from stationapp.infrastructure.golden_rig.sqlite_binding_store import SqliteGoldenRigBindingStore
        from stationapp.services.golden_rig_authorization import GoldenRigAuthorizationService
        from stationapp.services.golden_rig_secure_binding import SecureGoldenRigBindingService
        from stationapp.services.golden_rig_service import GoldenRigService

        # No LAN allocation client exists yet: the cache is filled by
        # importing server-issued allocation documents.
        gateway = None
        rig_bindings = SecureGoldenRigBindingService(
            settings.station_id,
            SqliteGoldenRigBindingStore(settings.database_url),
        )
        rf_tester = GoldenRigService(
            GoldenRigAuthorizationService(rig_bindings, settings.golden_rig_ca_certificate)
        )
        from stationapp.drivers.pricol.functional import PricolFunctionalTester

        functional = PricolFunctionalTester(
            baudrate=settings.pricol_uart_baudrate,
            response_timeout_seconds=settings.pricol_response_timeout_seconds,
            reset_wait_seconds=settings.pricol_reset_wait_seconds,
            start_message=settings.pricol_start_message,
            dtr=settings.pricol_uart_dtr,
            rts=settings.pricol_uart_rts,
        )
        programmer_factory = _mpcli_programmer_factory(settings)
        golden_rig_check = approved_golden_rig_check(rig_bindings, settings.golden_rig_ca_certificate)

    mac_cache = MacCacheService(
        session_factory=session_factory,
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        jig_positions=settings.jig_positions,
        threshold=max(settings.mac_cache_threshold, settings.jig_positions),
        gateway=gateway,
        request_size=settings.mac_request_size,
    )

    readiness = StationReadinessService(
        settings=settings,
        topology_service=topology_service,
        eligibility_service=eligibility_service,
        mac_cache=mac_cache,
        golden_rig_check=golden_rig_check,
        simulation=simulation,
        functional_test_available=True,
        session_check=access.session_health,
        sync_check=lambda: _sync_health(sync_scheduler),
        firmware_provider=lambda: (production_settings.current().stock_firmware, production_settings.current().pricol_firmware),
    )

    production = ProductionController(
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        slot_count=settings.jig_positions,
        store=SqlProductionStore(session_factory),
        programmer_factory=programmer_factory,
        rf_tester=rf_tester,
        functional_tester=functional,
        readiness_gate=readiness.gate,
        qr_pattern=settings.qr_pattern,
        require_firmware=not simulation,
        access=access,
        qr_claims=qr_claims,
        enabled_slots=lambda: production_settings.current().enabled_slots,
        qr_format=ScanFormat(length=settings.qr_code_length, hex_only=settings.qr_hex_only),
    )

    return production, readiness, mac_cache


def _reference_mac_reader(settings: Settings):
    """MPCLI MAC read (-I -r) for binding slots by reference modules.

    None in simulation or when MPCLI is not installed: the button is then
    disabled.
    """
    if settings.simulation_mode or not Path(settings.mpcli_path).is_file():
        return None
    from stationapp.drivers.mpcli.driver import MpCliDriver
    from stationapp.drivers.mpcli.process import MpCliProcessRunner

    driver = MpCliDriver(MpCliProcessRunner(Path(settings.mpcli_path)))
    return lambda com_port: driver.read_mac_structured(com_port=com_port, timeout_seconds=20.0)


def _mpcli_programmer_factory(settings: Settings):
    from stationapp.drivers.mpcli.configuration import MpCliProgrammingProfile
    from stationapp.drivers.mpcli.driver import MpCliDriver
    from stationapp.drivers.mpcli.process import MpCliProcessRunner
    from stationapp.services.programming import ProgrammingService

    def profile(image: Path) -> MpCliProgrammingProfile:
        if not Path(image).is_file():
            raise ValueError(f"Firmware image not found: {image}")
        return MpCliProgrammingProfile(image_packet=Path(image), chip_erase=settings.mpcli_chip_erase)

    def factory(stock: Path | None, pricol: Path | None) -> ProgrammingService:
        if stock is None:
            raise ValueError("Stock firmware image is required")
        return ProgrammingService(
            driver=MpCliDriver(MpCliProcessRunner(Path(settings.mpcli_path))),
            stock_profile=profile(stock),
            pricol_profile=profile(pricol) if pricol is not None else None,
            programming_timeout_seconds=settings.mpcli_programming_timeout_seconds,
            two_step=settings.mpcli_two_step,
        )

    return factory


def _app_version() -> str:
    from stationapp import __version__
    return __version__
