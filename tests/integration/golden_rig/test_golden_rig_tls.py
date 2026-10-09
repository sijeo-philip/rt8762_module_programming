
from __future__ import annotations

import hashlib
import ipaddress
import threading

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import (ExtendedKeyUsageOID, NameOID)
from stationapp.domain.golden_rig_binding import (GoldenRigBinding)
from stationapp.infrastructure.golden_rig.secure_transport import ( GoldenRigAuthenticationError, SecureGoldenRigTransport)
from stationapp.infrastructure.golden_rig.tls_test_server import (GoldenRigTlsTestServer)
from stationapp.infrastructure.golden_rig.transport import (GoldenRigCancelledError)
from stationapp.infrastructure.golden_rig.sqlite_binding_store import (
    SqliteGoldenRigBindingStore,
)
from stationapp.services.golden_rig_secure_binding import (
    AuthenticatedSupervisor,
    SecureGoldenRigBindingService,
)
from stationapp.services.golden_rig_authorization import (
    GoldenRigAuthorizationService,
)


def create_test_certificates(directory: Path, *, server_name: str = "127.0.0.1"):
    """Generate a fresh test CA and signed TLS server certificate."""

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key( public_exponent=65537, key_size=2048)

    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RTL8762 Golden Rig Test CA")])

    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    server_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GOLDEN-RIG-TEST-01")])

    try:
        san = x509.IPAddress(ipaddress.ip_address(server_name))
    except ValueError:
        san = x509.DNSName(server_name)

    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(ca_certificate.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "test_ca.pem"
    cert_path = directory / "server_cert.pem"
    key_path = directory / "server_key.pem"

    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))

    cert_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))

    key_path.write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    fingerprint = hashlib.sha256(server_certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ca_path, cert_path, key_path, fingerprint


def make_binding(port: int, fingerprint: str, *, host: str = "127.0.0.1") -> GoldenRigBinding:

    return GoldenRigBinding(
        station_id="STATION-01",
        rig_id="GOLDEN-RIG-TEST-01",
        host=host,
        port=port,
        certificate_sha256=fingerprint,
        approved_by="SUP-001",
        approved_at=datetime.now(timezone.utc),
    )

@pytest.mark.integration
def test_authenticated_tls_roundtrip(tmp_path: Path) -> None:

    ca, cert, key, fingerprint = (create_test_certificates(tmp_path))
    server = GoldenRigTlsTestServer(
        certificate=cert,
        private_key=key,
        responder=lambda request: b"ACK:" + request,
    )
    server.start()
    try:
        assert server.port is not None
        binding = make_binding(server.port, fingerprint)

        transport = SecureGoldenRigTransport(
            binding=binding,
            ca_certificate=ca,
            connect_timeout=2.0,
            io_timeout=2.0,
            operation_timeout=5.0,
        )

        response = transport.exchange(b"HELLO-GOLDEN-RIG")
        assert response == b"ACK:HELLO-GOLDEN-RIG"

    finally:
        server.stop()

    assert server.received == [b"HELLO-GOLDEN-RIG"]
    assert server.errors == []

@pytest.mark.integration
def test_unapproved_rig_certificate_rejected(tmp_path: Path) -> None:

    ca, cert, key, fingerprint = (create_test_certificates(tmp_path))
    server = GoldenRigTlsTestServer(
        certificate=cert,
        private_key=key,
        responder=lambda payload: b"ACK",
    )
    server.start()
    try:
        assert server.port is not None
        binding = make_binding(server.port,"ab" * 32)
        transport = SecureGoldenRigTransport(
            binding=binding,
            ca_certificate=ca,
            connect_timeout=2.0,
            operation_timeout=5.0,
        )

        with pytest.raises(GoldenRigAuthenticationError):
            transport.exchange(b"PRODUCTION-MAC-LIST")

    finally:
        server.stop()
    assert server.received == []

@pytest.mark.integration
def test_wrong_certificate_hostname_rejected(
    tmp_path: Path,
) -> None:

    ca, cert, key, fingerprint = (
        create_test_certificates(
            tmp_path,
            server_name="127.0.0.2",
        )
    )

    server = GoldenRigTlsTestServer(
        certificate=cert,
        private_key=key,
        responder=lambda payload: b"ACK",
    )

    server.start()

    try:
        assert server.port is not None

        binding = make_binding(
            server.port,
            fingerprint,
            host="127.0.0.1",
        )

        transport = SecureGoldenRigTransport(
            binding=binding,
            ca_certificate=ca,
            connect_timeout=2.0,
            operation_timeout=5.0,
        )

        with pytest.raises(
            GoldenRigAuthenticationError
        ):
            transport.exchange(b"HELLO")

    finally:
        server.stop()

    assert server.received == []


@pytest.mark.integration
def test_supervisor_binding_to_tls_roundtrip(tmp_path: Path) -> None:

    ca, cert, key, fingerprint = (create_test_certificates(tmp_path))

    server = GoldenRigTlsTestServer(
        certificate=cert,
        private_key=key,
        responder=lambda payload: b"RF-ACK:" + payload,
    )

    server.start()
    try:
        assert server.port is not None
        database = tmp_path / "station.db"
        repository = SqliteGoldenRigBindingStore(
            f"sqlite:///{database.as_posix()}"
        )
        repository.create_schema_for_tests()
        binding_service = SecureGoldenRigBindingService(
            station_id="STATION-01",
            repository=repository,
        )
        audit = binding_service.approve(
            supervisor=AuthenticatedSupervisor(
                user_id="SUP-001"
            ),
            rig_id="GOLDEN-RIG-TEST-01",
            host="127.0.0.1",
            port=server.port,
            certificate_sha256=fingerprint,
            reason="Golden Rig commissioning test",
        )

        # Reconstruct the service to simulate
        # loading the binding in a new application session.

        restarted_binding_service = (
            SecureGoldenRigBindingService(
                station_id="STATION-01",
                repository=repository,
            )
        )

        authorization = GoldenRigAuthorizationService(
            binding_service=restarted_binding_service,
            ca_certificate=ca,
        )

        transport = authorization.create_transport()
        response = transport.exchange(
            b"COMMISSIONING-REQUEST"
        )
        assert response == (
            b"RF-ACK:COMMISSIONING-REQUEST"
        )
        history = repository.list_audits("STATION-01")
        assert len(history) == 1
        assert history[0]["audit_id"] == audit.audit_id
        approved = restarted_binding_service.require_binding()
        assert approved.rig_id == "GOLDEN-RIG-TEST-01"
        assert approved.certificate_sha256 == fingerprint

    finally:
        server.stop()
    assert server.received == [b"COMMISSIONING-REQUEST"]
    assert server.errors == []
