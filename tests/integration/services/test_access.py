"""Login, lockout, user administration and production sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.data.models import AuditRecordModel, UserModel
from stationapp.domain.batch import ProgrammingMode
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AuthenticationFailed, AuthorizationError, UserRole
from stationapp.services.access import (
    AuthenticationService,
    SecretHasher,
    StationAccessService,
    UserAdministrationService,
)
from stationapp.services.production import ProductionController, ProductionError, SlotPort
from stationapp.data.production_repository import SqlProductionStore
from stationapp.services.mac_cache import MacCacheService
from stationapp.domain.mac import MacPurpose
from stationapp.services.simulation import (
    SimulatedFunctionalTester,
    SimulatedGoldenRig,
    SimulatedMacAllocationGateway,
    SimulatedProgrammer,
)

STATION = "STATION-01"

# Fast hashing keeps the suite quick; production uses argon2 defaults.
FAST = SecretHasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def config(*, slots=((1, "USB:A"), (2, "USB:B")), rig=("RIG-1", "a" * 64)) -> StationConfiguration:
    return StationConfiguration(
        jig_id="JIG-01",
        jig_positions=4,
        slot_bindings=tuple(slots),
        golden_rig_id=rig[0],
        golden_rig_fingerprint=rig[1],
    )


@pytest.fixture
def env(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    clock = Clock()
    live = {"config": config()}

    auth = AuthenticationService(session_factory=factory, station_id=STATION, hasher=FAST, max_failures=3, lockout=timedelta(minutes=5), clock=clock)
    users = UserAdministrationService(session_factory=factory, station_id=STATION, hasher=FAST)
    access = StationAccessService(
        session_factory=factory,
        station_id=STATION,
        authentication=auth,
        configuration_provider=lambda: live["config"],
        supervisor_timeout=timedelta(minutes=15),
        clock=clock,
    )

    supervisor = users.create_first_supervisor(user_id="sup1", display_name="Asha", password="supervisor-pw")
    admin = auth.authenticate("SUP1", "supervisor-pw", purpose="test")
    users.create_user(admin, user_id="op1", display_name="Ravi", role=UserRole.OPERATOR, secret="1234", badge_id="BADGE-77", reason="New operator")

    yield {"factory": factory, "clock": clock, "live": live, "auth": auth, "users": users, "access": access, "supervisor": supervisor}
    engine.dispose()


def audit_actions(factory) -> list[str]:
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditRecordModel).order_by(AuditRecordModel.id))]


@pytest.mark.integration
def test_first_supervisor_only_when_station_has_no_users(env):
    with pytest.raises(AuthorizationError):
        env["users"].create_first_supervisor(user_id="sup2", display_name="X", password="password-2")


@pytest.mark.integration
def test_secrets_are_hashed_never_stored(env):
    with env["factory"]() as session:
        row = session.get(UserModel, "OP1")
        assert row.secret_hash.startswith("$argon2")
        assert "1234" not in row.secret_hash


@pytest.mark.integration
def test_wrong_password_locks_account_after_limit(env):
    auth, clock = env["auth"], env["clock"]
    for _ in range(3):
        with pytest.raises(AuthenticationFailed):
            auth.authenticate("op1", "0000", purpose="test")

    with pytest.raises(AuthenticationFailed, match="locked"):
        auth.authenticate("op1", "1234", purpose="test")

    clock.now += timedelta(minutes=6)
    assert auth.authenticate("op1", "1234", purpose="test").user_id == "OP1"
    assert "ACCOUNT_LOCKED" in audit_actions(env["factory"])


@pytest.mark.integration
def test_operator_cannot_log_in_before_session(env):
    with pytest.raises(AuthorizationError, match="Supervisor must start"):
        env["access"].login_operator("op1", "1234")


@pytest.mark.integration
def test_session_lifecycle_and_operator_badge(env):
    access = env["access"]

    with pytest.raises(AuthorizationError, match="Supervisor login required"):
        access.start_session(config())

    with pytest.raises(AuthorizationError, match="not a Supervisor"):
        access.login_supervisor("op1", "1234")

    access.login_supervisor("sup1", "supervisor-pw")
    session = access.start_session(config())
    assert session.supervisor_id == "SUP1"

    operator = access.login_operator_badge("BADGE-77")
    assert operator.user_id == "OP1"
    assert access.require_production_access() == (session.session_id, "OP1")

    with pytest.raises(AuthenticationFailed, match="not recognised"):
        access.login_operator_badge("nope")

    supervisor = access.require_supervisor()
    env["users"].set_badge(supervisor, user_id="sup1", badge_id="BADGE-SUP", reason="Issued badge")
    with pytest.raises(AuthenticationFailed, match="password"):
        access.login_operator_badge("BADGE-SUP")

    access.close_session("Shift end")
    assert access.session is None
    assert access.operator is None
    actions = audit_actions(env["factory"])
    assert "SESSION_STARTED" in actions and "SESSION_CLOSED" in actions


@pytest.mark.integration
def test_session_survives_restart_but_logins_do_not(env):
    access = env["access"]
    access.login_supervisor("sup1", "supervisor-pw")
    session = access.start_session(config())
    access.login_operator("op1", "1234")

    restarted = StationAccessService(
        session_factory=env["factory"],
        station_id=STATION,
        authentication=env["auth"],
        configuration_provider=lambda: env["live"]["config"],
    )

    assert restarted.session.session_id == session.session_id
    assert restarted.operator is None


@pytest.mark.integration
def test_configuration_change_blocks_production_until_reconfirmed(env):
    access, live = env["access"], env["live"]
    access.login_supervisor("sup1", "supervisor-pw")
    access.start_session(config())
    access.login_operator("op1", "1234")

    live["config"] = config(slots=((1, "USB:A"), (2, "USB:OTHER")))

    with pytest.raises(AuthorizationError, match="slot\\(s\\) 2"):
        access.require_production_access()

    access.reconfirm_configuration("Replaced slot 2 adapter")
    assert access.require_production_access()[1] == "OP1"


@pytest.mark.integration
def test_session_start_rejects_stale_review_and_missing_rig(env):
    access, live = env["access"], env["live"]
    access.login_supervisor("sup1", "supervisor-pw")

    reviewed = config()
    live["config"] = config(rig=(None, None))
    with pytest.raises(AuthorizationError, match="changed during review"):
        access.start_session(reviewed)

    with pytest.raises(AuthorizationError, match="Golden Rig"):
        access.start_session(live["config"])


@pytest.mark.integration
def test_supervisor_login_times_out(env):
    access, clock = env["access"], env["clock"]
    access.login_supervisor("sup1", "supervisor-pw")
    clock.now += timedelta(minutes=16)

    assert access.supervisor is None
    with pytest.raises(AuthorizationError):
        access.require_supervisor()


@pytest.mark.integration
def test_session_cannot_close_during_active_batch(env):
    access = env["access"]
    access.login_supervisor("sup1", "supervisor-pw")
    access.start_session(config())
    access.set_batch_activity_probe(lambda: True)

    with pytest.raises(AuthorizationError, match="batch"):
        access.close_session("Shift end")


@pytest.mark.integration
def test_user_administration_rules(env):
    users = env["users"]
    sup = env["auth"].authenticate("sup1", "supervisor-pw", purpose="test")
    op = env["auth"].authenticate("op1", "1234", purpose="test")

    with pytest.raises(AuthorizationError):
        users.create_user(op, user_id="op2", display_name="Y", role=UserRole.OPERATOR, secret="1234", badge_id=None, reason="r")

    with pytest.raises(ValueError, match="at least 8"):
        users.create_user(sup, user_id="sup2", display_name="Y", role=UserRole.SUPERVISOR, secret="short", badge_id=None, reason="r")

    with pytest.raises(ValueError, match="badge"):
        users.create_user(sup, user_id="op3", display_name="Z", role=UserRole.OPERATOR, secret="9999", badge_id="BADGE-77", reason="r")

    with pytest.raises(AuthorizationError, match="your own account"):
        users.set_active(sup, user_id="sup1", active=False, reason="r")

    users.set_active(sup, user_id="op1", active=False, reason="Left company")
    with pytest.raises(AuthenticationFailed, match="disabled"):
        env["auth"].authenticate("op1", "1234", purpose="test")


@pytest.mark.integration
def test_production_requires_logged_in_operator_and_records_it(env):
    access, factory = env["access"], env["factory"]
    cache = MacCacheService(session_factory=factory, station_id=STATION, jig_id="JIG-01", jig_positions=4, threshold=8, gateway=SimulatedMacAllocationGateway(), request_size=16)
    cache.ensure(MacPurpose.STOCK_RF_TEST)

    none = frozenset()
    controller = ProductionController(
        station_id=STATION,
        jig_id="JIG-01",
        slot_count=4,
        store=SqlProductionStore(factory),
        programmer=SimulatedProgrammer(delay_seconds=0, failures=none),
        rf_tester=SimulatedGoldenRig(delay_seconds=0, failures=none),
        functional_tester=SimulatedFunctionalTester(delay_seconds=0, failures=none),
        access=access,
    )
    ports = {1: SlotPort(1, "USB:A", "COM5")}

    with pytest.raises(ProductionError, match="No production session"):
        controller.start_batch(occupied=(1,), ports=ports, mode=ProgrammingMode.STOCK_ONLY)

    access.login_supervisor("sup1", "supervisor-pw")
    session = access.start_session(config())
    with pytest.raises(ProductionError, match="Operator login required"):
        controller.start_batch(occupied=(1,), ports=ports, mode=ProgrammingMode.STOCK_ONLY)

    access.login_operator("op1", "1234")
    batch = controller.start_batch(occupied=(1,), ports=ports, mode=ProgrammingMode.STOCK_ONLY, operator_id="typed-name-ignored")
    assert batch.operator_id == "OP1"
    assert batch.session_id == session.session_id

    access.logout_operator()
    with pytest.raises(ProductionError, match="Operator login required"):
        controller.advance()

    with pytest.raises(ProductionError, match="Supervisor authorization"):
        controller.abort_batch(reason="test")
    controller.abort_batch(reason="test", supervisor=access.authorize_supervisor("sup1", "supervisor-pw"))
