from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stationapp.domain.mac import (MacAddress, MacPurpose)
from stationapp.drivers.mpcli.configuration import ( MpCliProgrammingProfile)
from stationapp.drivers.mpcli.driver import (MpCliDriver)
from stationapp.drivers.mpcli.process import (MpCliProcessRunner)
from stationapp.drivers.mpcli.types import (IdentityOutcome, MacReadbackOutcome, ProgrammingOutcome)
from stationapp.services.programming import (ProgrammingService, ProgrammingTarget)


VALID_KEY = (
    "00112233445566778899AABBCCDDEEFF"
    "00112233445566778899AABBCCDDEEFF"
)


@pytest.fixture
def stub_path() -> Path:

    return (
        Path(__file__)
        .parents[3]
        / "fixtures"
        / "mpcli"
        / "stub_mpcli.py"
    )


@pytest.fixture
def profiles(tmp_path: Path):
    stock_image = (tmp_path/ "stock.bin")
    pricol_image = (tmp_path/ "pricol.bin")
    stock_image.write_bytes(b"stock-image")
    pricol_image.write_bytes(b"pricol-image")

    stock = MpCliProgrammingProfile(image_packet=stock_image, product_id="11223344", secret_key=VALID_KEY)

    pricol = MpCliProgrammingProfile(image_packet=pricol_image, product_id="11223344", secret_key=VALID_KEY)
    return stock, pricol


@pytest.fixture
def driver(stub_path: Path) -> MpCliDriver:

    runner = MpCliProcessRunner(
        Path(sys.executable),
        base_arguments=(str(stub_path),),
        termination_grace_seconds=0.2,
        poll_interval_seconds=0.01,
    )

    return MpCliDriver(runner)


@pytest.fixture
def service(driver: MpCliDriver, profiles) -> ProgrammingService:
    stock, pricol = profiles
    return ProgrammingService(
        driver=driver,
        stock_profile=stock,
        pricol_profile=pricol,
        programming_timeout_seconds=2.0,
        readback_timeout_seconds=2.0,
    )


def make_stock_target() -> ProgrammingTarget:
    return ProgrammingTarget(
        batch_id="BATCH-001",
        slot_number=1,
        com_port="COM7",
        mac=MacAddress.parse(
            "AABBCCDDEE01"
        ),
        purpose=(
            MacPurpose.STOCK_RF_TEST
        ),
    )


def make_pricol_target() -> ProgrammingTarget:
    return ProgrammingTarget(
        batch_id="BATCH-001",
        slot_number=1,
        com_port="COM7",
        mac=MacAddress.parse(
            "112233445566"
        ),
        purpose=(
            MacPurpose.PRICOL_PRODUCTION
        ),
    )

@pytest.mark.integration
def test_stock_programming_success_end_to_end(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "success")

    result = service.program_stock(make_stock_target())

    assert (result.programming.outcome is ProgrammingOutcome.SUCCESS)

    assert result.programming.succeeded is True

    assert (result.programming.process.return_code == 0)

    assert ("Programming completed" in result.programming.process.stdout)

@pytest.mark.integration
def test_pricol_programming_success_end_to_end(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "success")

    result = service.program_pricol(make_pricol_target())

    assert (result.programming.outcome is ProgrammingOutcome.SUCCESS)


@pytest.mark.integration
def test_programming_failure_end_to_end(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "failure")
    result = service.program_stock(make_stock_target())

    assert (result.programming.outcome is ProgrammingOutcome.FAILED)
    assert (result.programming.process.return_code == 3) 

@pytest.mark.integration
def test_no_device_is_failure(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "no_device")

    result = service.program_stock(make_stock_target())

    assert (result.programming.outcome is ProgrammingOutcome.FAILED)
    assert ("device not detected" in result.programming.process.stderr)

@pytest.mark.integration
def test_hung_programming_becomes_uncertain(driver: MpCliDriver, profiles, monkeypatch) -> None:

    stock, pricol = profiles

    monkeypatch.setenv("STUB_MPCLI_MODE", "timeout")
    service = ProgrammingService(
        driver=driver,
        stock_profile=stock,
        pricol_profile=pricol,
        programming_timeout_seconds=0.25,
        readback_timeout_seconds=1.0,
    )

    result = service.program_stock(make_stock_target())
    assert (result.programming.outcome is ProgrammingOutcome.UNCERTAIN)
    assert (result.programming.uncertain is True)

@pytest.mark.integration
def test_pricol_readback_match_end_to_end(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "readback")
    monkeypatch.setenv("STUB_MPCLI_READBACK_MAC", "112233445566")

    result = service.verify_pricol_mac(make_pricol_target())

    assert (result.readback.outcome is MacReadbackOutcome.READ)
    assert (result.verification.outcome is IdentityOutcome.MATCH)
    assert result.verification.matched is True               


@pytest.mark.integration
def test_pricol_readback_mismatch_end_to_end(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "readback")
    monkeypatch.setenv("STUB_MPCLI_READBACK_MAC", "112233445567")
    result = service.verify_pricol_mac(make_pricol_target())
    assert (result.verification.outcome is IdentityOutcome.MISMATCH)
    assert result.verification.matched is False


@pytest.mark.integration
def test_ambiguous_readback_is_not_accepted(service: ProgrammingService, monkeypatch) -> None:

    monkeypatch.setenv("STUB_MPCLI_MODE", "ambiguous_readback")

    result = service.verify_pricol_mac(make_pricol_target())
    assert (result.readback.outcome is MacReadbackOutcome.AMBIGUOUS)
    assert (result.verification.outcome is IdentityOutcome.NOT_VERIFIED)
    assert result.verification.matched is False

@pytest.mark.integration
def test_version_command_end_to_end(driver: MpCliDriver) -> None:
    result = driver.version()
    assert result.succeeded is True
    assert ("1.0.4.25" in result.stdout)


