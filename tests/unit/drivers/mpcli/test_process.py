from __future__ import annotations

import sys
from pathlib import Path

from stationapp.concurrency.cancellation import (CancellationToken)
from stationapp.drivers.mpcli.process import (MpCliProcessRunner)
from stationapp.drivers.mpcli.types import (ProcessOutcome)


def make_python_runner() -> MpCliProcessRunner:

    return MpCliProcessRunner(
        Path(sys.executable),
        termination_grace_seconds=0.2,
        poll_interval_seconds=0.01,
    )


def test_process_captures_stdout() -> None:
    runner = make_python_runner()
    result = runner.run(
        (
            "-c",
            "print('HELLO-MPCLI-STUB')",
        ),
        timeout_seconds=2.0,
    )

    assert (
        result.outcome
        is ProcessOutcome.COMPLETED
    )

    assert result.return_code == 0

    assert (
        "HELLO-MPCLI-STUB"
        in result.stdout
    )


def test_process_captures_stderr() -> None:
    runner = make_python_runner()
    result = runner.run(
        (
            "-c",
            (
                "import sys; "
                "print('ERROR-TEXT', file=sys.stderr)"
            ),
        ),
        timeout_seconds=2.0,
    )

    assert result.return_code == 0
    assert (
        "ERROR-TEXT"
        in result.stderr
    )


def test_process_reports_nonzero_exit() -> None:
    runner = make_python_runner()
    result = runner.run(
        (
            "-c",
            "raise SystemExit(7)",
        ),
        timeout_seconds=2.0,
    )

    assert (
        result.outcome
        is ProcessOutcome.COMPLETED
    )

    assert result.return_code == 7
    assert result.succeeded is False


def test_hung_process_times_out() -> None:
    runner = make_python_runner()
    result = runner.run(
        (
            "-c",
            (
                "import time; "
                "time.sleep(30)"
            ),
        ),
        timeout_seconds=0.2,
    )

    assert (
        result.outcome
        is ProcessOutcome.TIMED_OUT
    )
    assert result.succeeded is False
    assert result.duration_seconds < 5.0


def test_cancelled_process_is_stopped() -> None:
    runner = make_python_runner()
    token = CancellationToken()
    token.cancel()
    result = runner.run(
        (
            "-c",
            (
                "import time; "
                "time.sleep(30)"
            ),
        ),
        timeout_seconds=10.0,
        cancellation_token=token,
    )

    assert (
        result.outcome
        is ProcessOutcome.CANCELLED
    )

    assert result.succeeded is False

def test_process_runner_supports_base_arguments(
    tmp_path: Path,
) -> None:

    script = (
        tmp_path
        / "stub.py"
    )

    script.write_text(
        "import sys\n"
        "print('ARGS=' + '|'.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    runner = MpCliProcessRunner(
        Path(sys.executable),
        base_arguments=(
            str(script),
        ),
    )

    result = runner.run(
        (
            "-V",
        ),
        timeout_seconds=2.0,
    )

    assert result.succeeded is True

    assert "ARGS=-V" in result.stdout