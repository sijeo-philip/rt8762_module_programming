from stationapp.domain.mac import (
    MacAddress,
)
from stationapp.drivers.mpcli.parsers import (
    parse_mac_readback,
    parse_programming_result,
    compare_readback,
)
from stationapp.drivers.mpcli.types import (
    MacReadbackOutcome,
    ProcessOutcome,
    ProcessResult,
    ProgrammingOutcome,
    IdentityOutcome,
)


def make_process(
    *,
    outcome: ProcessOutcome = ProcessOutcome.COMPLETED,
    return_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
) -> ProcessResult:

    return ProcessResult(
        command=(
            "mpcli.exe",
        ),
        outcome=outcome,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
    )


def test_zero_exit_without_failure_is_programming_success() -> None:

    result = parse_programming_result(
        make_process(
            stdout="Programming completed"
        )
    )

    assert (
        result.outcome
        is ProgrammingOutcome.SUCCESS
    )

    assert result.succeeded is True


def test_nonzero_exit_is_programming_failure() -> None:

    result = parse_programming_result(
        make_process(
            return_code=7,
            stderr="program failed",
        )
    )

    assert (
        result.outcome
        is ProgrammingOutcome.FAILED
    )


def test_failure_text_overrides_zero_return_code() -> None:

    result = parse_programming_result(
        make_process(
            stdout=(
                "Programming operation failed"
            )
        )
    )

    assert (
        result.outcome
        is ProgrammingOutcome.FAILED
    )


def test_timeout_is_uncertain() -> None:

    result = parse_programming_result(
        make_process(
            outcome=ProcessOutcome.TIMED_OUT,
            return_code=-15,
        )
    )

    assert (
        result.outcome
        is ProgrammingOutcome.UNCERTAIN
    )

    assert result.uncertain is True


def test_cancellation_is_uncertain() -> None:

    result = parse_programming_result(
        make_process(
            outcome=ProcessOutcome.CANCELLED,
            return_code=-15,
        )
    )

    assert (
        result.outcome
        is ProgrammingOutcome.UNCERTAIN
    )


def test_labelled_compact_mac_is_read() -> None:

    result = parse_mac_readback(
        make_process(
            stdout=(
                "MAC: AABBCCDDEE01"
            )
        )
    )

    assert (
        result.outcome
        is MacReadbackOutcome.READ
    )

    assert result.mac == MacAddress.parse(
        "AA:BB:CC:DD:EE:01"
    )


def test_labelled_colon_mac_is_read() -> None:

    result = parse_mac_readback(
        make_process(
            stdout=(
                "BT Address: "
                "AA:BB:CC:DD:EE:01"
            )
        )
    )

    assert result.readable is True

    assert result.mac == MacAddress.parse(
        "AABBCCDDEE01"
    )


def test_mac_can_be_found_in_stderr() -> None:

    result = parse_mac_readback(
        make_process(
            stderr=(
                "BD_ADDR = "
                "11:22:33:44:55:66"
            )
        )
    )

    assert result.mac == MacAddress.parse(
        "112233445566"
    )


def test_unlabelled_hex_is_not_accepted_as_mac() -> None:

    result = parse_mac_readback(
        make_process(
            stdout=(
                "001122334455"
            )
        )
    )

    assert (
        result.outcome
        is MacReadbackOutcome.NOT_FOUND
    )

    assert result.mac is None


def test_multiple_distinct_macs_are_ambiguous() -> None:

    result = parse_mac_readback(
        make_process(
            stdout=(
                "MAC: AABBCCDDEE01\n"
                "BT MAC: AABBCCDDEE02"
            )
        )
    )

    assert (
        result.outcome
        is MacReadbackOutcome.AMBIGUOUS
    )

    assert result.mac is None


def test_same_mac_repeated_is_not_ambiguous() -> None:

    result = parse_mac_readback(
        make_process(
            stdout=(
                "MAC: AABBCCDDEE01\n"
                "BT MAC: AA:BB:CC:DD:EE:01"
            )
        )
    )

    assert (
        result.outcome
        is MacReadbackOutcome.READ
    )

    assert result.mac == MacAddress.parse(
        "AABBCCDDEE01"
    )


def test_readback_nonzero_exit_is_failure() -> None:

    result = parse_mac_readback(
        make_process(
            return_code=3,
            stderr="communication error",
        )
    )

    assert (
        result.outcome
        is MacReadbackOutcome.PROCESS_FAILED
    )


def test_readback_timeout_is_uncertain() -> None:

    result = parse_mac_readback(
        make_process(
            outcome=ProcessOutcome.TIMED_OUT,
            return_code=-15,
        )
    )

    assert (
        result.outcome
        is MacReadbackOutcome.UNCERTAIN
    )


def test_readback_match_helper() -> None:

    expected = MacAddress.parse(
        "AABBCCDDEE01"
    )

    result = parse_mac_readback(
        make_process(
            stdout=(
                "MAC = AABBCCDDEE01"
            )
        )
    )

    assert (
        result.matches(
            expected
        )
        is True
    )


def test_readback_mismatch_helper() -> None:

    expected = MacAddress.parse(
        "AABBCCDDEE01"
    )

    result = parse_mac_readback(
        make_process(
            stdout=(
                "MAC = AABBCCDDEE02"
            )
        )
    )

    assert (
        result.matches(
            expected
        )
        is False
    )

def test_identity_match() -> None:

    expected = MacAddress.parse(
        "AABBCCDDEE01"
    )

    readback = parse_mac_readback(
        make_process(
            stdout=(
                "BT Address: "
                "AA:BB:CC:DD:EE:01"
            )
        )
    )

    verification = compare_readback(
        expected=expected,
        readback=readback,
    )

    assert (
        verification.outcome
        is IdentityOutcome.MATCH
    )

    assert verification.matched is True

def test_identity_mismatch() -> None:

    expected = MacAddress.parse(
        "AABBCCDDEE01"
    )

    readback = parse_mac_readback(
        make_process(
            stdout=(
                "BT Address: "
                "AA:BB:CC:DD:EE:02"
            )
        )
    )

    verification = compare_readback(
        expected=expected,
        readback=readback,
    )

    assert (
        verification.outcome
        is IdentityOutcome.MISMATCH
    )

    assert verification.matched is False

def test_missing_readback_is_not_verified() -> None:

    expected = MacAddress.parse(
        "AABBCCDDEE01"
    )

    readback = parse_mac_readback(
        make_process(
            stdout="No device information"
        )
    )

    verification = compare_readback(
        expected=expected,
        readback=readback,
    )

    assert (
        verification.outcome
        is IdentityOutcome.NOT_VERIFIED
    )



