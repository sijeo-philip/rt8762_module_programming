"""Station configuration: bind slots from reference modules (Supervisor only)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AccessError
from stationapp.drivers.serial.types import DiscoveredSerialPort, UsbSerialIdentity
from stationapp.services.access import AuthenticationService, SecretHasher, StationAccessService, UserAdministrationService
from stationapp.services.jig_configuration import JigConfigurationService
from stationapp.services.slot_binding import SlotBindingService

STATION = "STATION-01"
FAST = SecretHasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


def port(device: str, serial: str) -> DiscoveredSerialPort:
    return DiscoveredSerialPort(device, UsbSerialIdentity(0x0403, 0x6001, serial, None), description="USB Serial Port")


@pytest.fixture
def station(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    auth = AuthenticationService(session_factory=factory, station_id=STATION, hasher=FAST)
    users = UserAdministrationService(session_factory=factory, station_id=STATION, hasher=FAST)
    users.create_first_supervisor(user_id="sup1", display_name="Asha", password="supervisor-pw")
    access = StationAccessService(
        session_factory=factory, station_id=STATION, authentication=auth,
        configuration_provider=lambda: StationConfiguration("JIG-01", 4, (), None, None),
        supervisor_timeout=timedelta(minutes=15),
    )
    # Jig: COM numbers deliberately not in slot order.
    ports = [port("COM4", "AAA"), port("COM6", "BBB"), port("COM7", "CCC"), port("COM8", "DDD")]
    reference = {"COM4": "22:22:22:22:22:03", "COM6": "22:22:22:22:22:01", "COM7": "22:22:22:22:22:04", "COM8": "22:22:22:22:22:02"}
    bindings = SlotBindingService(binding_file=tmp_path / "bindings.json", station_id=STATION, jig_id="JIG-01", jig_positions=4)
    jig = JigConfigurationService(
        station_id=STATION, binding_service=bindings, access=access, session_factory=factory,
        discover=lambda: tuple(ports),
        mac_reader=lambda device: SimpleNamespace(mac=reference.get(device), message="no MAC"),
    )
    yield SimpleNamespace(jig=jig, access=access, bindings=bindings, ports=ports, reference=reference, factory=factory)
    engine.dispose()


def audit_actions(factory) -> list[tuple[str, str]]:
    from stationapp.data.models import AuditRecordModel as AuditModel

    with factory() as session:
        rows = session.scalars(select(AuditModel).where(AuditModel.record_type == "JIG_SLOT_BINDING")).all()
        return [(row.record_id, row.action) for row in rows]


@pytest.mark.integration
def test_supervisor_binds_every_slot_from_reference_modules(station):
    with pytest.raises(AccessError):
        station.jig.scan_reference_modules()                     # Supervisor only

    station.access.login_supervisor("sup1", "supervisor-pw")
    plan = station.jig.scan_reference_modules()
    assert plan.complete
    assert station.bindings.load().bindings == ()                # the scan saves nothing

    configuration = station.jig.apply_reference_plan(plan, reason="Jig commissioning")

    keys = {binding.slot_number: binding.stable_key for binding in configuration.bindings}
    assert keys == {
        1: "USB:0403:6001:SERIAL:BBB",   # COM6 holds 22:22:22:22:22:01
        2: "USB:0403:6001:SERIAL:DDD",   # COM8
        3: "USB:0403:6001:SERIAL:AAA",   # COM4
        4: "USB:0403:6001:SERIAL:CCC",   # COM7
    }
    assert sorted(audit_actions(station.factory)) == [(f"SLOT-{n}", "SLOT_BOUND") for n in (1, 2, 3, 4)]


@pytest.mark.integration
def test_moved_interface_releases_its_old_slot(station):
    station.access.login_supervisor("sup1", "supervisor-pw")
    station.jig.apply_reference_plan(station.jig.scan_reference_modules(), reason="first")

    # Re-plugged: COM6's adapter now serves slot 3, COM4 has no module.
    station.reference.clear()
    station.reference["COM6"] = "22:22:22:22:22:03"
    plan = station.jig.scan_reference_modules()
    assert set(plan.assignments) == {3}

    keys = {b.slot_number: b.stable_key for b in station.jig.apply_reference_plan(plan, reason="re-plugged").bindings}
    assert keys[3] == "USB:0403:6001:SERIAL:BBB"
    assert 1 not in keys                                          # BBB left slot 1
    assert keys[2] == "USB:0403:6001:SERIAL:DDD" and keys[4] == "USB:0403:6001:SERIAL:CCC"   # untouched
    assert ("SLOT-1", "SLOT_UNBOUND") in audit_actions(station.factory)


@pytest.mark.integration
def test_apply_refuses_ports_that_disappeared(station):
    station.access.login_supervisor("sup1", "supervisor-pw")
    plan = station.jig.scan_reference_modules()
    station.ports.pop(0)                                         # COM4 unplugged before Apply
    with pytest.raises(ValueError, match="no longer detected"):
        station.jig.apply_reference_plan(plan, reason="x")
    assert station.bindings.load().bindings == ()
