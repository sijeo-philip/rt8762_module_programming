"""Safe child-process execution for Realtek MPCLI."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence

from stationapp.concurrency.cancellation import (CancellationToken)
from stationapp.drivers.mpcli.errors import (MpCliExecutableNotFound, MpCliProcessStartError)
from stationapp.drivers.mpcli.types import (ProcessOutcome, ProcessResult)


class MpCliProcessRunner:
    """Own and supervise one MPCLI child process.

    The runner never attempts to terminate the Python worker thread.

    Cancellation and timeout operate only on the external MPCLI process.
    """

    def __init__(self, executable: Path, *, termination_grace_seconds: float = 2.0, poll_interval_seconds: float = 0.05) -> None:

        self._executable = executable
        self._termination_grace_seconds = (
            termination_grace_seconds
        )
        self._poll_interval_seconds = (
            poll_interval_seconds
        )

    @property
    def executable(self) -> Path:
        return self._executable

    def run(self, arguments: Sequence[str], *, timeout_seconds: float, cancellation_token: CancellationToken | None = None) -> ProcessResult:
        """Execute MPCLI and capture all output.
        Timeout escalation:
            terminate()
               ↓
            grace period
                ↓
            kill()

        Only the child process is terminated.
        """

        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be positive"
            )

        if not self._executable.exists():
            raise MpCliExecutableNotFound(
                f"MPCLI executable not found: "
                f"{self._executable}"
            )
        command = (
            str(self._executable),
            *tuple(arguments),
        )
        start_time = time.monotonic()

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=self._creation_flags(),
            )

        except OSError as exc:
            raise MpCliProcessStartError(
                f"Failed to start MPCLI: {exc}"
            ) from exc

        outcome: ProcessOutcome | None = None
        deadline = (start_time + timeout_seconds)

        while process.poll() is None:
            if (
                cancellation_token is not None
                and cancellation_token.is_cancelled
            ):
                outcome = ProcessOutcome.CANCELLED

                self._stop_process(
                    process
                )

                break
            if time.monotonic() >= deadline:
                outcome = ProcessOutcome.TIMED_OUT
                self._stop_process(
                    process
                )
                break
            time.sleep(
                self._poll_interval_seconds
            )

        stdout, stderr = process.communicate()
        duration = (
            time.monotonic()
            - start_time
        )
        if outcome is None:
            outcome = ProcessOutcome.COMPLETED

        return ProcessResult(
            command=command,
            outcome=outcome,
            return_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=duration,
        )

    def _stop_process(self, process: subprocess.Popen[str]) -> None:
        """Terminate MPCLI, escalating to kill if needed."""
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(
                timeout=self._termination_grace_seconds
            )
            return

        except subprocess.TimeoutExpired:
            pass

        process.kill()
        process.wait()

    @staticmethod
    def _creation_flags() -> int:
        """Return Windows process flags when available."""

        return getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )


    