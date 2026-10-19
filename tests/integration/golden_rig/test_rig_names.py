"""Golden Rig by IP address or (mDNS) name: validation, resolution, TLS."""

from __future__ import annotations

import socket
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stationapp.domain.golden_rig_binding import GoldenRigBinding, GoldenRigBindingError, normalise_rig_host
from stationapp.infrastructure.golden_rig.resolver import (
    HostResolutionError,
    HostResolver,
    build_mdns_query,
    mdns_resolve,
    parse_mdns_a_records,
)
from stationapp.infrastructure.golden_rig.secure_transport import GoldenRigAuthenticationError, SecureGoldenRigTransport
from stationapp.infrastructure.golden_rig.tls_test_server import GoldenRigTlsTestServer
from stationapp.services.golden_rig_configuration import (
    GoldenRigIdentityError,
    GoldenRigIdentityProbe,
    RigEndpoint,
    parse_endpoints,
)
from tests.integration.golden_rig.test_golden_rig_tls import create_test_certificates
from tests.integration.services.test_golden_rig_configuration import HandshakeServer

RIG_NAME = "golden-rig-test.local"


# ---------------------------------------------------------------------------
# Accepted addresses
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("Golden-Rig-01.LOCAL.", "golden-rig-01.local"),
        (" 192.168.10.50 ", "192.168.10.50"),
        ("[fe80::1]", "fe80::1"),
        ("rig2.factory.lan", "rig2.factory.lan"),
    ],
)
def test_names_and_ips_are_accepted(raw, host):
    assert normalise_rig_host(raw) == host
    assert RigEndpoint(raw, 8762).host == host


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "mDNS name"),
        ("https://rig.local", "no http"),
        ("rig.local:8762", "port separately"),
        ("golden rig", "no http"),
        ("999.1.1.1", "not a valid IP"),
        ("-bad.local", "not a valid host name"),
    ],
)
def test_bad_addresses_are_rejected_clearly(raw, message):
    with pytest.raises(ValueError, match=message):
        normalise_rig_host(raw)


@pytest.mark.unit
def test_binding_stores_canonical_host():
    binding = GoldenRigBinding("S", "RIG-1", "Golden-Rig-01.Local", 8762, "a" * 64, "SUP", datetime.now(timezone.utc))
    assert binding.host == "golden-rig-01.local"
    with pytest.raises(GoldenRigBindingError):
        GoldenRigBinding("S", "RIG-1", "http://rig", 8762, "a" * 64, "SUP", datetime.now(timezone.utc))


@pytest.mark.unit
def test_candidates_accept_names_and_ipv6():
    assert parse_endpoints("golden-rig-01.local:8762,[fe80::1]:8762") == (
        RigEndpoint("golden-rig-01.local", 8762),
        RigEndpoint("fe80::1", 8762),
    )
    assert str(RigEndpoint("fe80::1", 8762)) == "[fe80::1]:8762"


# ---------------------------------------------------------------------------
# mDNS packet handling
# ---------------------------------------------------------------------------


def mdns_answer(name: str, address: str, *, transaction_id: int = 0, extra_name: str | None = None) -> bytes:
    """Response with the question echoed and A records using compression."""
    question = build_mdns_query(name, transaction_id)[12:]
    header = struct.pack("!HHHHHH", transaction_id, 0x8400, 1, 1 + (1 if extra_name else 0), 0, 0)
    pointer = b"\xc0\x0c"                                  # -> name in question
    answer = pointer + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(address)
    if extra_name:
        other = b"".join(bytes([len(p)]) + p.encode() for p in extra_name.split(".")) + b"\x00"
        answer += other + struct.pack("!HHIH", 1, 1, 120, 4) + socket.inet_aton("10.9.9.9")
    return header + question + answer


@pytest.mark.unit
def test_mdns_query_and_answer_parsing():
    query = build_mdns_query(RIG_NAME, 7)
    assert query[:2] == b"\x00\x07" and b"\x0fgolden-rig-test\x05local\x00" in query
    packet = mdns_answer(RIG_NAME, "192.168.10.50", extra_name="other.local")
    assert parse_mdns_a_records(packet, RIG_NAME) == ["192.168.10.50"]
    assert parse_mdns_a_records(packet, "GOLDEN-RIG-TEST.LOCAL.") == ["192.168.10.50"]
    assert parse_mdns_a_records(packet[:20], RIG_NAME) == []      # truncated: ignored safely
    assert parse_mdns_a_records(query, RIG_NAME) == []            # a query is not an answer


@pytest.mark.integration
def test_mdns_query_round_trip_with_local_responder():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    responder.settimeout(3)

    def answer_once():
        try:
            packet, sender = responder.recvfrom(9000)
            transaction_id = struct.unpack("!H", packet[:2])[0]
            responder.sendto(mdns_answer(RIG_NAME, "192.168.10.77", transaction_id=transaction_id), sender)
        except OSError:
            pass

    thread = threading.Thread(target=answer_once, daemon=True)
    thread.start()
    try:
        assert mdns_resolve(RIG_NAME, 2.0, group=responder.getsockname()) == ["192.168.10.77"]
    finally:
        thread.join(3)
        responder.close()


# ---------------------------------------------------------------------------
# Resolver order
# ---------------------------------------------------------------------------


def failing_system(*args, **kwargs):
    raise socket.gaierror(11001, "getaddrinfo failed")


@pytest.mark.unit
def test_resolver_order():
    calls = []
    resolver = HostResolver(system=failing_system, mdns=lambda name, timeout: calls.append(name) or ["10.0.0.5"])
    assert resolver.resolve("192.168.1.9", 8762) == ["192.168.1.9"]       # IP: no lookup
    assert resolver.resolve(RIG_NAME, 8762) == ["10.0.0.5"]               # .local -> mDNS fallback
    assert calls == [RIG_NAME]

    system = lambda host, port, type: [(socket.AF_INET, type, 6, "", ("10.1.1.1", port))]  # noqa: E731
    assert HostResolver(system=system, mdns=lambda *a: ["never"]).resolve(RIG_NAME, 8762) == ["10.1.1.1"]

    with pytest.raises(HostResolutionError, match="UDP port 5353"):
        HostResolver(system=failing_system, mdns=lambda *a: []).resolve(RIG_NAME, 8762)
    with pytest.raises(HostResolutionError, match="rig.factory.lan"):
        HostResolver(system=failing_system, mdns=lambda *a: ["x"]).resolve("rig.factory.lan", 8762)


# ---------------------------------------------------------------------------
# TLS by name: connect to the resolved IP, verify the certificate's name
# ---------------------------------------------------------------------------


@pytest.fixture
def named_rig(tmp_path: Path):
    ca, cert, key, fingerprint = create_test_certificates(tmp_path, server_name=RIG_NAME)
    resolver = HostResolver(system=failing_system, mdns=lambda name, timeout: ["127.0.0.1"])
    return ca, cert, key, fingerprint, resolver


@pytest.mark.integration
def test_identity_probe_by_mdns_name(named_rig):
    ca, cert, key, fingerprint, resolver = named_rig
    server = HandshakeServer(cert, key)
    try:
        probe = GoldenRigIdentityProbe(ca_certificate=ca, resolver=resolver, timeout=2.0)
        identity = probe.probe(RigEndpoint(RIG_NAME, server.port))
        assert identity.certificate_sha256 == fingerprint
        assert identity.address == "127.0.0.1"
        assert identity.endpoint_label == f"{RIG_NAME}:{server.port} (127.0.0.1)"

        # A different name at the same IP is refused: the certificate
        # does not cover it.
        with pytest.raises(GoldenRigIdentityError, match="not trusted"):
            probe.probe(RigEndpoint("impostor-rig.local", server.port))
    finally:
        server.close()


@pytest.mark.integration
def test_rf_transport_by_mdns_name(named_rig):
    ca, cert, key, fingerprint, resolver = named_rig
    server = GoldenRigTlsTestServer(certificate=cert, private_key=key, responder=lambda request: b"ACK:" + request)
    server.start()
    try:
        binding = GoldenRigBinding("S", "GOLDEN-RIG-TEST-01", RIG_NAME, server.port, fingerprint, "SUP", datetime.now(timezone.utc))
        transport = SecureGoldenRigTransport(binding=binding, ca_certificate=ca, resolver=resolver, connect_timeout=2.0, io_timeout=2.0, operation_timeout=5.0)
        assert transport.exchange(b"hello") == b"ACK:hello"
    finally:
        server.stop()


@pytest.mark.integration
def test_rf_transport_rejects_name_not_in_certificate(named_rig):
    ca, cert, key, fingerprint, resolver = named_rig
    server = GoldenRigTlsTestServer(certificate=cert, private_key=key, responder=lambda request: request)
    server.start()
    try:
        binding = GoldenRigBinding("S", "GOLDEN-RIG-TEST-01", "impostor-rig.local", server.port, fingerprint, "SUP", datetime.now(timezone.utc))
        transport = SecureGoldenRigTransport(binding=binding, ca_certificate=ca, resolver=resolver, connect_timeout=2.0, io_timeout=2.0, operation_timeout=5.0)
        with pytest.raises(GoldenRigAuthenticationError):
            transport.exchange(b"hello")
    finally:
        server.stop()


@pytest.mark.unit
def test_ipv4_addresses_are_tried_first():
    system = lambda host, port, type: [  # noqa: E731
        (socket.AF_INET6, type, 6, "", ("fe80::1", port, 0, 0)),
        (socket.AF_INET, type, 6, "", ("192.168.1.36", port)),
    ]
    assert HostResolver(system=system).resolve(RIG_NAME, 8762) == ["192.168.1.36", "fe80::1"]
