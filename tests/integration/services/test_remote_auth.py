"""Server-verified logins, test users, and Supervisor production settings."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.data.models import AuditRecordModel
from stationapp.data.production_repository import SqlProductionStore
from stationapp.domain.batch import ProgrammingMode
from stationapp.domain.mac import MacPurpose
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AuthenticationFailed, AuthorizationError, UserRole
from stationapp.drivers.lan.client import LanServerClient
from stationapp.services.access import StationAccessService
from stationapp.services.mac_cache import MacCacheService
from stationapp.services.production import ProductionController, ProductionError, SlotPort
from stationapp.services.production_settings import ProductionSettings, ProductionSettingsService
from stationapp.services.remote_auth import ServerAuthenticationService
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedMacAllocationGateway,
    SimulatedProgrammer,
)
from stationapp.services.test_users import TestAuthServer, TestUser

STATION = "STN-07"
CONFIG = StationConfiguration("JIG-07", 4, ((1, "USB:A"), (2, "USB:B")), "RIG-1", "a" * 64)


@pytest.fixture
def env(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    def make_auth(transport=None, *, users=None):
        server = TestAuthServer(users) if users else TestAuthServer()
        client = LanServerClient(base_url="http://lan", api_key=None, station_id=STATION, transport=transport or server.transport())
        return ServerAuthenticationService(client=client, session_factory=factory, station_id=STATION, jig_id="JIG-07", mode_description="test", test_users=True)

    auth = make_auth()
    access = StationAccessService(session_factory=factory, station_id=STATION, authentication=auth, configuration_provider=lambda: CONFIG)
    yield {"factory": factory, "auth": auth, "access": access, "make_auth": make_auth, "tmp": tmp_path}
    engine.dispose()


# ---------------------------------------------------------------------------
# Server-verified login
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_hardcoded_test_users_log_in_with_server_roles(env):
    auth = env["auth"]
    operator = auth.authenticate("operator1", "op1234", purpose="test")
    supervisor = auth.authenticate("SUPERVISOR1", "Sup@1234", purpose="test")
    assert (operator.user_id, operator.role) == ("OPERATOR1", UserRole.OPERATOR)
    assert (supervisor.user_id, supervisor.role) == ("SUPERVISOR1", UserRole.SUPERVISOR)
    assert auth.authenticate_badge("OPBADGE001", purpose="test").user_id == "OPERATOR1"


@pytest.mark.integration
def test_wrong_password_and_server_lockout(env):
    auth = env["auth"]
    for _ in range(5):
        with pytest.raises(AuthenticationFailed, match="Invalid user ID or password"):
            auth.authenticate("OPERATOR1", "wrong", purpose="test")
    with pytest.raises(AuthenticationFailed, match="locked"):
        auth.authenticate("OPERATOR1", "op1234", purpose="test")

    with env["factory"]() as session:
        actions = [row.action for row in session.scalars(select(AuditRecordModel))]
    assert actions.count("LOGIN_FAILED") == 6


@pytest.mark.integration
def test_supervisor_badge_is_refused_by_station(env):
    users = (TestUser("SUP9", "Badge Supervisor", "SUPERVISOR", "pw-12345", badge_id="SUPBADGE"),)
    auth = env["make_auth"](users=users)
    with pytest.raises(AuthenticationFailed, match="Supervisors must log in with a password"):
        auth.authenticate_badge("SUPBADGE", purpose="test")


@pytest.mark.integration
def test_unreachable_server_gives_clear_message(env):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    auth = env["make_auth"](httpx.MockTransport(down))
    with pytest.raises(AuthenticationFailed, match="Cannot reach the LAN server"):
        auth.authenticate("OPERATOR1", "op1234", purpose="test")


@pytest.mark.integration
def test_no_server_configured_refuses_login(env):
    auth = ServerAuthenticationService(client=None, session_factory=env["factory"], station_id=STATION, jig_id="J", mode_description="none")
    with pytest.raises(AuthenticationFailed, match="not configured"):
        auth.authenticate("OPERATOR1", "op1234", purpose="test")


@pytest.mark.integration
def test_single_login_routes_by_role(env):
    access = env["access"]

    with pytest.raises(AuthorizationError, match="Supervisor must log in and start one"):
        access.login("OPERATOR1", "op1234")

    supervisor = access.login("supervisor1", "Sup@1234")
    assert access.supervisor == supervisor and access.operator is None
    access.start_session(CONFIG)
    access.logout_current()
    assert access.supervisor is None

    operator = access.login_with_badge("OPBADGE001")
    assert access.operator == operator
    access.logout_current()
    assert access.operator is None


# ---------------------------------------------------------------------------
# Production settings
# ---------------------------------------------------------------------------


def _settings_service(env, *, access=None):
    firmware = env["tmp"] / "stock.bin"
    firmware.write_bytes(b"fw")
    defaults = ProductionSettings(ProgrammingMode.STOCK_AND_PRICOL, firmware, None, (1, 2, 3, 4))
    return ProductionSettingsService(session_factory=env["factory"], station_id=STATION, jig_positions=4, defaults=defaults, access=access), firmware


@pytest.mark.integration
def test_settings_require_supervisor_and_are_audited(env):
    access = env["access"]
    service, firmware = _settings_service(env, access=access)
    new = ProductionSettings(ProgrammingMode.STOCK_ONLY, firmware, None, (1, 3), lot="LOT-42")

    with pytest.raises(AuthorizationError, match="Supervisor login required"):
        service.update(new, reason="r")

    access.login("SUPERVISOR1", "Sup@1234")
    with pytest.raises(ValueError, match="Pricol firmware"):
        service.update(ProductionSettings(ProgrammingMode.STOCK_AND_PRICOL, firmware, None, (1,)), reason="r")
    with pytest.raises(ValueError, match="at least one"):
        service.update(ProductionSettings(ProgrammingMode.STOCK_ONLY, firmware, None, ()), reason="r")

    service.update(new, reason="Stock-only trial lot")

    reloaded, _ = _settings_service(env)
    assert reloaded.current() == new
    with env["factory"]() as session:
        audit = session.scalar(select(AuditRecordModel).where(AuditRecordModel.record_type == "PRODUCTION_SETTINGS"))
    assert audit.user_id == "SUPERVISOR1"
    assert audit.previous_value["mode"] == "STOCK_AND_PRICOL" and audit.new_value["enabled_slots"] == [1, 3]


@pytest.mark.integration
def test_settings_locked_during_batch_and_disabled_slots_refused(env):
    access = env["access"]
    access.login("SUPERVISOR1", "Sup@1234")
    access.start_session(CONFIG)
    service, firmware = _settings_service(env, access=access)
    service.update(ProductionSettings(ProgrammingMode.STOCK_ONLY, firmware, None, (1, 2)), reason="two slots")

    MacCacheService(session_factory=env["factory"], station_id=STATION, jig_id="JIG-07", jig_positions=4, threshold=8, gateway=SimulatedMacAllocationGateway(), request_size=8).ensure(MacPurpose.STOCK_RF_TEST)
    none = frozenset()
    controller = ProductionController(
        station_id=STATION, jig_id="JIG-07", slot_count=4,
        store=SqlProductionStore(env["factory"]),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=none),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=none),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=none),
        access=access,
        enabled_slots=lambda: service.current().enabled_slots,
    )
    access.set_batch_activity_probe(lambda: controller.has_active_batch)
    ports = {n: SlotPort(n, f"USB:{n}", f"COM{n}") for n in (1, 2, 3)}

    with pytest.raises(ProductionError, match="Slot\\(s\\) 3 are disabled"):
        controller.start_batch(occupied=(1, 3), ports=ports, mode=ProgrammingMode.STOCK_ONLY)

    controller.start_batch(occupied=(1, 2), ports=ports, mode=ProgrammingMode.STOCK_ONLY)
    with pytest.raises(AuthorizationError, match="current batch"):
        service.update(ProductionSettings(ProgrammingMode.STOCK_ONLY, firmware, None, (1,)), reason="mid-batch")
