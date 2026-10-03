"""Conservative parsers for Realtek MPCLI output.

The parser converts raw subprocess results into manufacturing-safe,
typed results.

Important:
    We intentionally avoid guessing success from vague text.

    Process timeout or cancellation after programming begins is always
    classified as UNCERTAIN because the device may have been modified
    before communication stopped.
"""

from __future__ import annotations

import re

from stationapp.domain.mac import (MacAddress)
from stationapp.drivers.mpcli.types import (
    MacReadbackOutcome,
    MacReadbackResult,
    ProcessOutcome,
    ProcessResult,
    ProgrammingOutcome,
    ProgrammingResult,
    IdentityVerificationResult,
    IdentityOutcome,
)


# ---------------------------------------------------------------------------
# Failure indicators
# ---------------------------------------------------------------------------

_FAILURE_PATTERNS = (
    re.compile(
        r"\bfail(?:ed|ure)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\berror\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bexception\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\btimeout\b",
        re.IGNORECASE,
    ),
)


# ---------------------------------------------------------------------------
# MAC labels
#
# We deliberately require a MAC-related label rather than simply extracting
# every 12-hex-digit number from MPCLI output.
#
# Firmware logs may contain addresses, keys, EUIDs and other hexadecimal
# values that must never be mistaken for a programmed Bluetooth address.
# ---------------------------------------------------------------------------

_MAC_LABEL_PATTERN = re.compile(
    r"""
    (?:
        MAC
        |
        BT[\s_-]*(?:MAC|ADDR|ADDRESS)
        |
        BD[\s_-]*(?:ADDR|ADDRESS)
        |
        BD_ADDR
    )
    [^0-9A-Fa-f]*
    (
        (?:
            [0-9A-Fa-f]{2}
            (?:
                :
                |
                -
                |
                \s
            )
        ){5}
        [0-9A-Fa-f]{2}
        |
        [0-9A-Fa-f]{12}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_programming_result(process: ProcessResult) -> ProgrammingResult:
    """Interpret one MPCLI programming subprocess result.

    Rules:

        timeout/cancel/forced termination
            -> UNCERTAIN

        process did not complete
            -> UNCERTAIN

        non-zero exit
            -> FAILED

        explicit failure/error text
            -> FAILED

        exit code 0 and no failure evidence
            -> SUCCESS

    SUCCESS here means only:
        MPCLI completed its programming request without known failure.

    Device identity still requires the later verification gate.
    """

    if process.outcome in {
        ProcessOutcome.TIMED_OUT,
        ProcessOutcome.CANCELLED,
        ProcessOutcome.TERMINATED,
    }:
        return ProgrammingResult(
            outcome=ProgrammingOutcome.UNCERTAIN,
            process=process,
            message=(
                "MPCLI programming did not finish "
                "with a known outcome."
            ),
        )

    if process.outcome is ProcessOutcome.START_FAILED:
        return ProgrammingResult(
            outcome=ProgrammingOutcome.FAILED,
            process=process,
            message=(
                "MPCLI could not be started."
            ),
        )

    if process.outcome is not ProcessOutcome.COMPLETED:
        return ProgrammingResult(
            outcome=ProgrammingOutcome.UNCERTAIN,
            process=process,
            message=(
                "MPCLI process outcome is not "
                "definitively complete."
            ),
        )

    if process.return_code != 0:
        return ProgrammingResult(
            outcome=ProgrammingOutcome.FAILED,
            process=process,
            message=(
                "MPCLI exited with return code "
                f"{process.return_code}."
            ),
        )

    output = _combined_output(
        process
    )

    failure = _find_failure_indicator(
        output
    )

    if failure is not None:
        return ProgrammingResult(
            outcome=ProgrammingOutcome.FAILED,
            process=process,
            message=(
                "MPCLI output contains a failure "
                f"indicator: {failure!r}."
            ),
        )

    return ProgrammingResult(
        outcome=ProgrammingOutcome.SUCCESS,
        process=process,
        message=(
            "MPCLI completed the programming command."
        ),
    )


def parse_mac_readback(process: ProcessResult) -> MacReadbackResult:
    """Extract a Bluetooth MAC from MPCLI -I output.

    The parser deliberately accepts only MAC-labelled values.

    A random 12-digit hexadecimal value is not sufficient evidence,
    because MPCLI may also print EUIDs, addresses or security data.
    """

    if process.outcome in {
        ProcessOutcome.TIMED_OUT,
        ProcessOutcome.CANCELLED,
        ProcessOutcome.TERMINATED,
    }:
        return MacReadbackResult(
            outcome=MacReadbackOutcome.UNCERTAIN,
            process=process,
            mac=None,
            message=(
                "MAC read-back did not complete "
                "with a known outcome."
            ),
        )

    if process.outcome is not ProcessOutcome.COMPLETED:
        return MacReadbackResult(
            outcome=MacReadbackOutcome.PROCESS_FAILED,
            process=process,
            mac=None,
            message=(
                "MPCLI read-back process did not "
                "complete normally."
            ),
        )

    if process.return_code != 0:
        return MacReadbackResult(
            outcome=MacReadbackOutcome.PROCESS_FAILED,
            process=process,
            mac=None,
            message=(
                "MPCLI read-back exited with return code "
                f"{process.return_code}."
            ),
        )

    output = _combined_output(process)

    failure = _find_failure_indicator(output)

    if failure is not None:
        return MacReadbackResult(
            outcome=MacReadbackOutcome.PROCESS_FAILED,
            process=process,
            mac=None,
            message=(
                "MPCLI read-back output contains "
                f"a failure indicator: {failure!r}."
            ),
        )

    candidates = _extract_labelled_macs(output)

    if not candidates:
        return MacReadbackResult(
            outcome=MacReadbackOutcome.NOT_FOUND,
            process=process,
            mac=None,
            message=(
                "MPCLI completed but no labelled "
                "Bluetooth MAC was found in its output."
            ),
        )

    if len(candidates) > 1:
        return MacReadbackResult(
            outcome=MacReadbackOutcome.AMBIGUOUS,
            process=process,
            mac=None,
            message=(
                "MPCLI output contained more than one "
                "distinct MAC candidate."
            ),
        )

    mac = next(iter(candidates))
    return MacReadbackResult(
        outcome=MacReadbackOutcome.READ,
        process=process,
        mac=mac,
        message=(
            f"MPCLI reported MAC {mac.compact}."
        ),
    )


def _combined_output(process: ProcessResult) -> str:

    return "\n".join(
        part
        for part in (
            process.stdout,
            process.stderr,
        )
        if part
    )


def _find_failure_indicator(text: str) -> str | None:

    for pattern in _FAILURE_PATTERNS:
        match = pattern.search(text)

        if match is not None:
            return match.group(0)

    return None

def _extract_labelled_macs(text: str) -> set[MacAddress]:
    candidates: set[MacAddress] = set()
    for match in _MAC_LABEL_PATTERN.finditer(text):
        raw = match.group(1)
        try:
            mac = MacAddress.parse(raw)

        except Exception:
            continue
        candidates.add(mac)
    return candidates

def verify_readback_mac(
    result: MacReadbackResult,
    *,
    expected: MacAddress,
) -> bool:
    """Return True only for definite identity equality."""

    return result.matches(
        expected
    )

def compare_readback(
    *,
    expected: MacAddress,
    readback: MacReadbackResult,
) -> IdentityVerificationResult:
    """Compare one allocated MAC against MPCLI evidence."""

    if not readback.readable:
        return IdentityVerificationResult(
            outcome=IdentityOutcome.NOT_VERIFIED,
            expected=expected,
            reported=None,
            message=(
                "Device identity could not be verified "
                "because no definite MAC was read back."
            ),
        )

    assert readback.mac is not None

    if readback.mac == expected:
        return IdentityVerificationResult(
            outcome=IdentityOutcome.MATCH,
            expected=expected,
            reported=readback.mac,
            message=(
                "Read-back MAC matches the "
                "allocated production MAC."
            ),
        )

    return IdentityVerificationResult(
        outcome=IdentityOutcome.MISMATCH,
        expected=expected,
        reported=readback.mac,
        message=(
            "Read-back MAC does not match "
            "the allocated production MAC."
        ),
    )
