"""The Golden Rig certificate tool makes certificates the station accepts."""

from __future__ import annotations

import pytest

from stationapp.infrastructure.golden_rig.tls_test_server import GoldenRigTlsTestServer
from stationapp.services.golden_rig_configuration import (
    GoldenRigIdentityError,
    GoldenRigIdentityProbe,
    RigEndpoint,
)
from stationapp.tools import golden_rig_certs as tool


def _request(tmp_path, **overrides):
    values = dict(
        rig_id="GOLDEN-RIG-07",
        dns_names=tool.dns_names_for("localhost"),
        ips=tool.check_ips(["127.0.0.1"]),
        out_dir=tmp_path / "out",
        ca_dir=tmp_path / "out" / "CA",
    )
    values.update(overrides)
    return tool.Request(**values)


def _probe(rig_dir, ca_file, host):
    server = GoldenRigTlsTestServer(
        certificate=rig_dir / tool.RIG_CERT,
        private_key=rig_dir / tool.RIG_KEY,
        responder=lambda payload: payload,
    )
    server.start()
    try:
        probe = GoldenRigIdentityProbe(ca_certificate=ca_file, inventory=["GOLDEN-RIG-07"])
        return probe.probe(RigEndpoint(host, server.port))
    finally:
        server.stop()


@pytest.mark.integration
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_station_probe_accepts_tool_certificate(tmp_path, host):
    rig_dir = tool.run(_request(tmp_path))

    identity = _probe(rig_dir, tmp_path / "out" / "CA" / tool.CA_CERT, host)

    assert identity.rig_id == "GOLDEN-RIG-07"
    assert identity.in_inventory
    cert = tool.x509.load_pem_x509_certificate((rig_dir / tool.RIG_CERT).read_bytes())
    assert identity.certificate_sha256 == tool.station_fingerprint(cert)
    info = (rig_dir / tool.INFO_FILE).read_text(encoding="utf-8")
    assert identity.certificate_sha256 in info
    assert (rig_dir / tool.CA_CERT).read_bytes() == (tmp_path / "out" / "CA" / tool.CA_CERT).read_bytes()


@pytest.mark.integration
def test_station_refuses_name_not_in_certificate(tmp_path):
    rig_dir = tool.run(_request(tmp_path, ips=[]))  # no 127.0.0.1 in the SAN

    with pytest.raises(GoldenRigIdentityError, match="not trusted"):
        _probe(rig_dir, tmp_path / "out" / "CA" / tool.CA_CERT, "127.0.0.1")


@pytest.mark.integration
def test_second_rig_reuses_the_line_ca(tmp_path):
    tool.run(_request(tmp_path))
    ca_before = (tmp_path / "out" / "CA" / tool.CA_CERT).read_bytes()

    tool.run(_request(tmp_path, rig_id="GOLDEN-RIG-08"))

    assert (tmp_path / "out" / "CA" / tool.CA_CERT).read_bytes() == ca_before
    assert (tmp_path / "out" / "GOLDEN-RIG-08" / tool.RIG_CERT).is_file()


@pytest.mark.integration
def test_existing_rig_certificate_not_replaced_without_confirmation(tmp_path):
    tool.run(_request(tmp_path))
    with pytest.raises(tool.CertToolError, match="Not replaced"):
        tool.run(_request(tmp_path))
    tool.run(_request(tmp_path), confirm=lambda question: True)


@pytest.mark.integration
def test_ca_certificate_without_key_is_refused(tmp_path):
    tool.run(_request(tmp_path))
    (tmp_path / "out" / "CA" / tool.CA_KEY).unlink()
    with pytest.raises(tool.CertToolError, match="key"):
        tool.run(_request(tmp_path, rig_id="GOLDEN-RIG-09"))


@pytest.mark.integration
def test_install_ca_backs_up_the_old_one(tmp_path):
    station = tmp_path / "StationApp"
    old = station / "config" / "certificates" / tool.CA_CERT
    old.parent.mkdir(parents=True)
    old.write_text("old ca", encoding="utf-8")

    tool.run(_request(tmp_path, install_ca=station))

    assert old.read_bytes() == (tmp_path / "out" / "CA" / tool.CA_CERT).read_bytes()
    assert [p.read_text(encoding="utf-8") for p in old.parent.glob("*.bak")] == ["old ca"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("golden-rig-01", ["golden-rig-01.local", "golden-rig-01"]),
        ("Golden-Rig-01.local", ["golden-rig-01.local", "golden-rig-01"]),
        ("rig1.plant.example.com", ["rig1.plant.example.com"]),
    ],
)
def test_dns_names_for(name, expected):
    assert tool.dns_names_for(name) == expected


@pytest.mark.unit
@pytest.mark.parametrize("name", ["", "http://rig:8762", "rig 01", "-rig", "rig_01"])
def test_dns_names_rejects(name):
    with pytest.raises(tool.CertToolError):
        tool.dns_names_for(name)


@pytest.mark.unit
def test_default_host_name_and_rig_id():
    assert tool.default_host_name("GOLDEN_RIG.01") == "golden-rig-01"
    assert tool.check_rig_id(" GOLDEN-RIG-01 ") == "GOLDEN-RIG-01"
    with pytest.raises(tool.CertToolError):
        tool.check_rig_id("GOLDEN RIG")
