
import hashlib

from datetime import datetime, timezone
from pathlib import Path

import pytest

from stationapp.domain.golden_rig_binding import (
    GoldenRigBinding,
)

from stationapp.infrastructure.golden_rig.secure_transport import (
    GoldenRigAuthenticationError,
    SecureGoldenRigTransport,
)


class FakeTlsPeer:
    def __init__(self, certificate: bytes):
        self.certificate = certificate

    def getpeercert(self, binary_form=False):
        assert binary_form is True
        return self.certificate


def make_transport(fingerprint: str):
    binding = GoldenRigBinding(
        station_id="STATION-01",
        rig_id="GOLDEN-RIG-01",
        host="golden-rig-01.factory.lan",
        port=8762,
        certificate_sha256=fingerprint,
        approved_by="SUP-001",
        approved_at=datetime.now(timezone.utc),
    )

    return SecureGoldenRigTransport(
        binding=binding,
        ca_certificate=Path("unused-in-unit-test.pem"),
    )


@pytest.mark.unit
def test_approved_certificate_fingerprint():
    certificate = b"SIMULATED-CERTIFICATE"
    fingerprint = hashlib.sha256(
        certificate
    ).hexdigest()

    transport = make_transport(fingerprint)

    transport._verify_certificate(
        FakeTlsPeer(certificate)
    )


@pytest.mark.unit
def test_unapproved_certificate_rejected():
    certificate = b"ANOTHER-RIG-CERTIFICATE"

    transport = make_transport(
        "ab" * 32
    )

    with pytest.raises(
        GoldenRigAuthenticationError
    ):
        transport._verify_certificate(
            FakeTlsPeer(certificate)
        )


@pytest.mark.unit
def test_missing_certificate_rejected():
    transport = make_transport(
        "ab" * 32
    )

    with pytest.raises(
        GoldenRigAuthenticationError
    ):
        transport._verify_certificate(
            FakeTlsPeer(b"")
        )
