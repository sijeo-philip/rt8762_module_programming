from stationapp.drivers.mpcli.types import (ProcessOutcome, ProcessResult)


def test_completed_zero_exit_is_success() -> None:
    result = ProcessResult(
        command=("mpcli.exe", "-V"),
        outcome=ProcessOutcome.COMPLETED,
        return_code=0,
        stdout="version",
        stderr="",
        duration_seconds=0.1,
    )

    assert result.completed is True
    assert result.succeeded is True


def test_timeout_is_not_success() -> None:

    result = ProcessResult(
        command=("mpcli.exe",),
        outcome=ProcessOutcome.TIMED_OUT,
        return_code=None,
        stdout="",
        stderr="",
        duration_seconds=10.0,
    )

    assert result.completed is False
    assert result.succeeded is False


def test_nonzero_exit_is_not_success() -> None:
    result = ProcessResult(
        command=("mpcli.exe",),
        outcome=ProcessOutcome.COMPLETED,
        return_code=1,
        stdout="",
        stderr="error",
        duration_seconds=0.1,
    )

    assert result.completed is True
    assert result.succeeded is False

    