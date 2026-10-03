from __future__ import annotations

from pathlib import Path

import pytest

from stationapp.domain.mac import (
    MacAddress,
    MacPurpose,
)
from stationapp.drivers.mpcli.configuration import (
    MpCliProgrammingProfile,
)
from stationapp.drivers.mpcli.types import (
    IdentityOutcome,
    MacReadbackOutcome,
    MacReadbackResult,
    ProcessOutcome,
    ProcessResult,
    ProgrammingOutcome,
    ProgrammingResult,
)
from stationapp.services.programming import (
    ProgrammingService,
    ProgrammingStage,
    ProgrammingTarget,
)


VALID_KEY = (
    "00112233445566778899AABBCCDDEEFF"
    "00112233445566778899AABBCCDDEEFF"
)


def make_process() -> ProcessResult:

    return ProcessResult(
        command=("mpcli.exe",),
        outcome=ProcessOutcome.COMPLETED,
        return_code=0,
        stdout="OK",
        stderr="",
        duration_seconds=0.1,
    )


def make_programming_success() -> ProgrammingResult:

    return ProgrammingResult(
        outcome=ProgrammingOutcome.SUCCESS,
        process=make_process(),
        message="success",
    )


class FakeDriver:

    def __init__(
        self,
        *,
        readback_mac: MacAddress | None = None,
    ) -> None:

        self.program_calls = []
        self.read_calls = []

        self.readback_mac = readback_mac

    def program(
        self,
        *,
        com_port,
        mac,
        profile,
        timeout_seconds,
        cancellation_token=None,
    ):

        self.program_calls.append(
            {
                "com_port": com_port,
                "mac": mac,
                "profile": profile,
                "timeout_seconds": timeout_seconds,
                "cancellation_token": cancellation_token,
            }
        )

        return make_programming_success()

    def read_mac_structured(
        self,
        *,
        com_port,
        baud,
        timeout_seconds,
        cancellation_token=None,
    ):

        self.read_calls.append(
            {
                "com_port": com_port,
                "baud": baud,
                "timeout_seconds": timeout_seconds,
                "cancellation_token": cancellation_token,
            }
        )

        if self.readback_mac is None:

            return MacReadbackResult(
                outcome=MacReadbackOutcome.NOT_FOUND,
                process=make_process(),
                mac=None,
                message="not found",
            )

        return MacReadbackResult(
            outcome=MacReadbackOutcome.READ,
            process=make_process(),
            mac=self.readback_mac,
            message="read",
        )


def make_profiles():

    stock = MpCliProgrammingProfile(
        image_packet=Path("stock.bin"),
        product_id="11223344",
        secret_key=VALID_KEY,
    )

    pricol = MpCliProgrammingProfile(
        image_packet=Path("pricol.bin"),
        product_id="11223344",
        secret_key=VALID_KEY,
    )

    return stock, pricol


def make_service(
    driver,
) -> ProgrammingService:

    stock, pricol = make_profiles()

    return ProgrammingService(
        driver=driver,
        stock_profile=stock,
        pricol_profile=pricol,
        programming_timeout_seconds=45.0,
        readback_timeout_seconds=8.0,
    )


def stock_target() -> ProgrammingTarget:

    return ProgrammingTarget(
        batch_id="BATCH-001",
        slot_number=1,
        com_port="COM7",
        mac=MacAddress.parse(
            "AA:BB:CC:DD:EE:01"
        ),
        purpose=MacPurpose.STOCK_RF_TEST,
    )


def pricol_target() -> ProgrammingTarget:

    return ProgrammingTarget(
        batch_id="BATCH-001",
        slot_number=1,
        com_port="COM7",
        mac=MacAddress.parse(
            "11:22:33:44:55:66"
        ),
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

def test_program_stock_uses_stock_profile() -> None:

    driver = FakeDriver()

    service = make_service(
        driver
    )

    result = service.program_stock(
        stock_target()
    )

    assert (
        result.stage
        is ProgrammingStage.STOCK
    )

    assert (
        result.programming.outcome
        is ProgrammingOutcome.SUCCESS
    )

    assert len(
        driver.program_calls
    ) == 1

    call = driver.program_calls[0]

    assert (
        call["profile"].image_packet
        == Path("stock.bin")
    )

    assert (
        call["mac"]
        == MacAddress.parse(
            "AABBCCDDEE01"
        )
    )

    assert call["com_port"] == "COM7"

    assert call["timeout_seconds"] == 45.0

def test_program_pricol_uses_pricol_profile() -> None:

    driver = FakeDriver()

    service = make_service(
        driver
    )

    result = service.program_pricol(
        pricol_target()
    )

    assert (
        result.stage
        is ProgrammingStage.PRICOL
    )

    call = driver.program_calls[0]

    assert (
        call["profile"].image_packet
        == Path("pricol.bin")
    )

    assert (
        call["mac"]
        == MacAddress.parse(
            "112233445566"
        )
    )

def test_stock_programming_rejects_pricol_mac() -> None:

    service = make_service(
        FakeDriver()
    )

    with pytest.raises(
        ValueError,
        match="expected STOCK_RF_TEST",
    ):
        service.program_stock(
            pricol_target()
        )

def test_pricol_programming_rejects_stock_mac() -> None:

    service = make_service(
        FakeDriver()
    )

    with pytest.raises(
        ValueError,
        match="expected PRICOL_PRODUCTION",
    ):
        service.program_pricol(
            stock_target()
        )

def test_pricol_readback_match() -> None:

    expected = MacAddress.parse(
        "112233445566"
    )

    driver = FakeDriver(
        readback_mac=expected
    )

    service = make_service(
        driver
    )

    result = service.verify_pricol_mac(
        pricol_target()
    )

    assert (
        result.verification.outcome
        is IdentityOutcome.MATCH
    )

    assert (
        result.verification.matched
        is True
    )

    assert result.readback.mac == expected

def test_pricol_readback_mismatch() -> None:

    driver = FakeDriver(
        readback_mac=MacAddress.parse(
            "112233445567"
        )
    )

    service = make_service(
        driver
    )

    result = service.verify_pricol_mac(
        pricol_target()
    )

    assert (
        result.verification.outcome
        is IdentityOutcome.MISMATCH
    )

    assert (
        result.verification.matched
        is False
    )

    assert (
        result.verification.expected
        == MacAddress.parse(
            "112233445566"
        )
    )

    assert (
        result.verification.reported
        == MacAddress.parse(
            "112233445567"
        )
    )

def test_pricol_readback_missing_is_not_verified() -> None:

    driver = FakeDriver(
        readback_mac=None
    )

    service = make_service(
        driver
    )

    result = service.verify_pricol_mac(
        pricol_target()
    )

    assert (
        result.verification.outcome
        is IdentityOutcome.NOT_VERIFIED
    )

    assert result.verification.matched is False

def test_readback_uses_pricol_profile_baud() -> None:

    driver = FakeDriver(
        readback_mac=MacAddress.parse(
            "112233445566"
        )
    )

    service = make_service(
        driver
    )

    service.verify_pricol_mac(
        pricol_target()
    )

    call = driver.read_calls[0]

    assert call["com_port"] == "COM7"

    assert call["baud"] == 1_000_000

    assert call["timeout_seconds"] == 8.0

def test_programming_target_requires_positive_slot() -> None:

    with pytest.raises(
        ValueError,
        match="slot_number",
    ):
        ProgrammingTarget(
            batch_id="BATCH-001",
            slot_number=0,
            com_port="COM7",
            mac=MacAddress.parse(
                "AABBCCDDEE01"
            ),
            purpose=MacPurpose.STOCK_RF_TEST,
        )


def test_programming_target_requires_batch() -> None:

    with pytest.raises(
        ValueError,
        match="batch_id",
    ):
        ProgrammingTarget(
            batch_id=" ",
            slot_number=1,
            com_port="COM7",
            mac=MacAddress.parse(
                "AABBCCDDEE01"
            ),
            purpose=MacPurpose.STOCK_RF_TEST,
        )


def test_programming_target_requires_com_port() -> None:

    with pytest.raises(
        ValueError,
        match="com_port",
    ):
        ProgrammingTarget(
            batch_id="BATCH-001",
            slot_number=1,
            com_port=" ",
            mac=MacAddress.parse(
                "AABBCCDDEE01"
            ),
            purpose=MacPurpose.STOCK_RF_TEST,
        )

class UncertainDriver(FakeDriver):

    def program(
        self,
        *,
        com_port,
        mac,
        profile,
        timeout_seconds,
        cancellation_token=None,
    ):

        process = ProcessResult(
            command=("mpcli.exe",),
            outcome=ProcessOutcome.TIMED_OUT,
            return_code=-15,
            stdout="",
            stderr="",
            duration_seconds=30.0,
        )

        return ProgrammingResult(
            outcome=ProgrammingOutcome.UNCERTAIN,
            process=process,
            message="timeout",
        )

def test_uncertain_programming_is_preserved() -> None:

    service = make_service(
        UncertainDriver()
    )

    result = service.program_stock(
        stock_target()
    )

    assert (
        result.programming.outcome
        is ProgrammingOutcome.UNCERTAIN
    )

    assert result.programming.uncertain is True

from stationapp.concurrency.cancellation import (
    CancellationToken,
)

def test_cancellation_token_is_forwarded() -> None:

    driver = FakeDriver()

    service = make_service(
        driver
    )

    token = CancellationToken()

    service.program_stock(
        stock_target(),
        cancellation_token=token,
    )

    assert (
        driver.program_calls[0][
            "cancellation_token"
        ]
        is token
    )



