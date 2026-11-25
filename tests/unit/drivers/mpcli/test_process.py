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

def test_large_output_does_not_stall_the_process() -> None:
    # Far more than a Windows pipe holds: an undrained pipe would block the
    # child until the timeout.
    runner = make_python_runner()
    result = runner.run(
        ("-c", "import sys\nfor i in range(4000): print('flash progress line', i)\nsys.stderr.write('E' * 20000)"),
        timeout_seconds=10.0,
    )
    assert result.outcome is ProcessOutcome.COMPLETED
    assert "flash progress line 3999" in result.stdout
    assert len(result.stderr) == 20000


def test_limiter_caps_concurrent_processes(tmp_path) -> None:
    import threading
    import time

    marker = tmp_path / "running"
    marker.mkdir()
    # Each child records itself while running, so the peak is visible.
    script = (
        "import os, sys, time, glob\n"
        "d = sys.argv[1]; me = os.path.join(d, str(os.getpid()))\n"
        "open(me, 'w').close()\n"
        "print(len(glob.glob(os.path.join(d, '*'))))\n"
        "time.sleep(0.4)\n"
        "os.remove(me)\n"
    )
    limiter = threading.BoundedSemaphore(2)
    runner = MpCliProcessRunner(Path(sys.executable), poll_interval_seconds=0.01, limiter=limiter)
    results = []

    def run() -> None:
        results.append(runner.run(("-c", script, str(marker)), timeout_seconds=1.5))

    started = time.monotonic()
    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(result.outcome is ProcessOutcome.COMPLETED for result in results)
    assert max(int(result.stdout.strip()) for result in results) <= 2
    # Waiting for a permit is not part of each run's 1.5 s timeout.
    assert time.monotonic() - started >= 1.2


def test_cancelled_while_waiting_for_a_permit() -> None:
    import threading

    limiter = threading.BoundedSemaphore(1)
    limiter.acquire()
    token = CancellationToken()
    token.cancel()
    runner = MpCliProcessRunner(Path(sys.executable), poll_interval_seconds=0.01, limiter=limiter)
    result = runner.run(("-c", "print('never')"), timeout_seconds=2.0, cancellation_token=token)
    assert result.outcome is ProcessOutcome.CANCELLED and result.stdout == ""
