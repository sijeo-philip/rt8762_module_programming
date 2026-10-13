"""Golden Rig binding screen services against real TLS endpoints."""

from __future__ import annotations

import socket
import ssl
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.domain.session import StationConfiguration
from stationapp.domain.users import AuthorizationError
from stationapp.infrastructure.golden_rig.sqlite_binding_store import SqliteGoldenRigBindingStore
from stationapp.services.access import AuthenticationService, SecretHasher, StationAccessService, UserAdministrationService
from stationapp.services.golden_rig_configuration import (
    GoldenRigConfigurationService,
    GoldenRigIdentityError,
    GoldenRigIdentityProbe,
    RigEndpoint,
    live_rig_configuration,
    parse_endpoints,
)
from stationapp.services.golden_rig_secure_binding import SecureGoldenRigBindingService
from tests.integration.golden_rig.test_golden_rig_tls import create_test_certificates

STATION = "STATION-01"
FAST = SecretHasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


class HandshakeServer:
    """Accepts any number of TLS handshakes, like an idle Golden Rig."""

    def __init__(self, certificate: Path, key: Path) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(certificate), str(key))
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            try:
                connection.settimeout(2.0)
                with self._context.wrap_socket(connection, server_side=True) as tls:
                    try:
                        tls.recv(1)
                    except (OSError, ssl.SSLError):
                        pass
            except (OSError, ssl.SSLError):
                pass

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=3)


@pytest.fixture
def rig_env(tmp_path: Path):
    trusted_dir = tmp_path / "trusted"
    trusted_dir.mkdir()
    ca, cert, key, fingerprint = create_test_certificates(trusted_dir)
    rogue_dir = tmp_path / "rogue"
    rogue_dir.mkdir()
    _rogue_ca, rogue_cert, rogue_key, _ = create_test_certificates(rogue_dir)

    rig = HandshakeServer(cert, key)
    rogue = HandshakeServer(rogue_cert, rogue_key)

    database_url = f"sqlite:///{(tmp_path / 'station.db').as_posix()}"
    engine = create_station_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    store = SqliteGoldenRigBindingStore(database_url)
    store.create_schema_for_tests()

    def live_configuration() -> StationConfiguration:
        rig_id, rig_fp = live_rig_configuration(store, STATION)
        return StationConfiguration("JIG-01", 4, ((1, "USB:A"),), rig_id, rig_fp)

    auth = AuthenticationService(session_factory=factory, station_id=STATION, hasher=FAST)
    UserAdministrationService(session_factory=factory, station_id=STATION, hasher=FAST).create_first_supervisor(user_id="sup1", display_name="Asha", password="supervisor-pw")

    probe = GoldenRigIdentityProbe(ca_certificate=ca, inventory=("GOLDEN-RIG-TEST-01",), timeout=2.0)
    access = StationAccessService(session_factory=factory, station_id=STATION, authentication=auth, configuration_provider=live_configuration, supervisor_timeout=timedelta(minutes=15))
    service = GoldenRigConfigurationService(
        station_id=STATION,
        store=store,
        binding_service=SecureGoldenRigBindingService(STATION, store),
        probe=probe,
        access=access,
        candidates=(RigEndpoint("127.0.0.1", rig.port), RigEndpoint("127.0.0.1", rogue.port)),
    )

    yield {"service": service, "access": access, "rig": rig, "rogue": rogue, "fingerprint": fingerprint, "ca": ca, "store": store, "probe": probe}

    rig.close()
    rogue.close()
    engine.dispose()


@pytest.mark.integration
def test_binding_screen_requires_supervisor(rig_env):
    with pytest.raises(AuthorizationError):
        rig_env["service"].discover()


@pytest.mark.integration
def test_discovery_separates_trusted_and_untrusted_rigs(rig_env):
    rig_env["access"].login_supervisor("sup1", "supervisor-pw")

    results = {result.endpoint.port: result for result in rig_env["service"].discover()}

    trusted = results[rig_env["rig"].port]
    assert trusted.identity.rig_id == "GOLDEN-RIG-TEST-01"
    assert trusted.identity.certificate_sha256 == rig_env["fingerprint"]
    assert trusted.identity.approvable

    rogue = results[rig_env["rogue"].port]
    assert rogue.identity is None
    assert "not trusted" in rogue.error


@pytest.mark.integration
def test_approve_bind_and_start_verified_session(rig_env):
    service, access = rig_env["service"], rig_env["access"]
    access.login_supervisor("sup1", "supervisor-pw")

    identity = service.request_identity(RigEndpoint("127.0.0.1", rig_env["rig"].port))
    audit = service.approve(identity, reason="Commissioning station 1")

    binding = service.current_binding()
    assert binding.rig_id == "GOLDEN-RIG-TEST-01"
    assert binding.certificate_sha256 == rig_env["fingerprint"]
    assert binding.approved_by == "SUP1"
    assert audit.action == "GOLDEN_RIG_BOUND"
    assert service.history()

    configuration = access.current_configuration()
    assert service.verify_bound_rig(configuration) == ()


@pytest.mark.integration
def test_rig_outside_inventory_cannot_be_approved(rig_env):
    access = rig_env["access"]
    access.login_supervisor("sup1", "supervisor-pw")
    strict = GoldenRigIdentityProbe(ca_certificate=rig_env["ca"], inventory=("OTHER-RIG",))
    identity = strict.probe(RigEndpoint("127.0.0.1", rig_env["rig"].port))

    assert not identity.approvable
    with pytest.raises(AuthorizationError, match="inventory"):
        rig_env["service"].approve(identity, reason="test")


@pytest.mark.integration
def test_session_verification_fails_when_rig_unreachable(rig_env):
    service, access = rig_env["service"], rig_env["access"]
    access.login_supervisor("sup1", "supervisor-pw")
    service.approve(service.request_identity(RigEndpoint("127.0.0.1", rig_env["rig"].port)), reason="Commissioning")
    configuration = access.current_configuration()

    rig_env["rig"].close()

    problems = service.verify_bound_rig(configuration)
    assert problems and "Cannot reach" in problems[0]


@pytest.mark.integration
def test_disabling_binding_is_audited_and_blocks_session_start(rig_env):
    service, access = rig_env["service"], rig_env["access"]
    access.login_supervisor("sup1", "supervisor-pw")
    service.approve(service.request_identity(RigEndpoint("127.0.0.1", rig_env["rig"].port)), reason="Commissioning")

    service.disable(reason="Rig sent for calibration")

    assert service.current_binding().enabled is False
    with pytest.raises(AuthorizationError, match="Golden Rig"):
        access.start_session(access.current_configuration())


@pytest.mark.integration
def test_unreachable_endpoint_reports_clearly(rig_env):
    probe = rig_env["probe"]
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    with pytest.raises(GoldenRigIdentityError, match="Cannot reach"):
        probe.probe(RigEndpoint("127.0.0.1", port))


@pytest.mark.unit
def test_parse_endpoints():
    assert parse_endpoints("10.0.0.5:7443, rig-2.local:7443") == (
        RigEndpoint("10.0.0.5", 7443),
        RigEndpoint("rig-2.local", 7443),
    )
    with pytest.raises(ValueError):
        parse_endpoints("no-port")
