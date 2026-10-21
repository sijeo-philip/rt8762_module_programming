"""Step-by-step Golden Rig connection check (bench 'Check connection')."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stationapp.domain.golden_rig_binding import GoldenRigBinding
from stationapp.infrastructure.golden_rig.resolver import HostResolver
from stationapp.services.golden_rig_check import GoldenRigConnectionCheck, RigCheckStatus
from tests.integration.golden_rig.test_golden_rig_tls import create_test_certificates
from tests.integration.services.test_golden_rig_configuration import HandshakeServer

RIG_NAME = "golden-rig-01.local"
PASS, FAIL, INFO = RigCheckStatus.PASS, RigCheckStatus.FAIL, RigCheckStatus.INFO


def failing_system(*args, **kwargs):
    raise socket.gaierror(11001, "getaddrinfo failed")


@pytest.fixture
def rig(tmp_path: Path):
    ca, cert, key, fingerprint = create_test_certificates(tmp_path, server_name=RIG_NAME)
    server = HandshakeServer(cert, key)
    # The name is only answered by "mDNS", as on a PC whose system
    # resolver does not know .local names.
    resolver = HostResolver(system=failing_system, mdns=lambda name, timeout: ["127.0.0.1"] if name == RIG_NAME else [])
    yield {"ca": ca, "fingerprint": fingerprint, "port": server.port, "resolver": resolver}
    server.close()


def make_check(rig, *, binding=None, ca=None):
    return GoldenRigConnectionCheck(ca_certificate=ca or rig["ca"], binding=lambda: binding, resolver=rig["resolver"], timeout=2.0)


def binding_for(rig, *, fingerprint=None, host=RIG_NAME):
    return GoldenRigBinding("S", "GOLDEN-RIG-TEST-01", host, rig["port"], fingerprint or rig["fingerprint"], "SUP", datetime.now(timezone.utc))


def statuses(result):
    return [(step.title, step.status) for step in result.steps]


@pytest.mark.integration
def test_approved_rig_reached_by_mdns_name(rig):
    identity_binding = binding_for(rig)
    result = make_check(rig, binding=identity_binding).check(RIG_NAME, rig["port"])

    assert result.ok and result.problem is None
    assert statuses(result) == [("Name lookup", PASS), ("Connection", PASS), ("Rig certificate", PASS), ("Approved binding", PASS)]
    assert "golden-rig-01.local -> 127.0.0.1 (via mDNS)" in result.steps[0].detail
    assert "open at 127.0.0.1" in result.steps[1].detail
    assert result.identity.rig_id == identity_binding.rig_id
    assert "Matches the approved rig" in result.steps[3].detail


@pytest.mark.integration
def test_reachable_rig_without_binding_is_info_only(rig):
    result = make_check(rig).check(RIG_NAME, rig["port"])
    assert result.ok
    assert result.steps[-1].status is INFO
    assert "no approved Golden Rig" in result.steps[-1].detail


@pytest.mark.integration
def test_different_certificate_than_approved_fails(rig):
    result = make_check(rig, binding=binding_for(rig, fingerprint="b" * 64)).check(RIG_NAME, rig["port"])
    assert not result.ok
    assert statuses(result)[-1] == ("Approved binding", FAIL)
    assert result.problem.startswith("Approved binding:")


@pytest.mark.integration
def test_unknown_name_fails_at_lookup(rig):
    result = make_check(rig).check("golden-rig-99.local", rig["port"])
    assert statuses(result) == [("Name lookup", FAIL)]
    assert "UDP port 5353" in result.problem


@pytest.mark.integration
def test_closed_port_fails_at_connection(rig):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    result = make_check(rig).check(RIG_NAME, closed_port)
    assert statuses(result) == [("Name lookup", PASS), ("Connection", FAIL)]
    assert "Golden Rig application running" in result.problem


@pytest.mark.integration
def test_name_not_in_certificate_fails_at_certificate(rig):
    resolver = HostResolver(system=failing_system, mdns=lambda name, timeout: ["127.0.0.1"])
    check = GoldenRigConnectionCheck(ca_certificate=rig["ca"], resolver=resolver, timeout=2.0)
    result = check.check("impostor-rig.local", rig["port"])
    assert statuses(result) == [("Name lookup", PASS), ("Connection", PASS), ("Rig certificate", FAIL)]
    assert "not trusted" in result.problem


@pytest.mark.integration
def test_missing_ca_still_reports_connection(rig, tmp_path):
    result = make_check(rig, ca=tmp_path / "missing.pem").check(RIG_NAME, rig["port"])
    assert statuses(result) == [("Name lookup", PASS), ("Connection", PASS), ("Rig certificate", FAIL)]
    assert "CA certificate not found" in result.problem


@pytest.mark.unit
def test_bad_address_and_ip_literal(rig):
    check = make_check(rig)
    assert statuses(check.check("https://golden-rig-01.local", 8762)) == [("Address", FAIL)]
    result = check.check("127.0.0.1", rig["port"])
    assert "no lookup needed" in result.steps[0].detail
