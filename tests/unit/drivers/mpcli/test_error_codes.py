"""MPCLI error-code explanations (Realtek MPCLI User Guide, Table 5.1)."""

from __future__ import annotations

import pytest

from stationapp.drivers.mpcli.error_codes import MPCLI_ERROR_CODES, describe, failed_step
from stationapp.drivers.mpcli.parsers import parse_mac_readback, parse_programming_result
from stationapp.drivers.mpcli.types import ProcessOutcome, ProcessResult

# Real output from a slot whose module did not answer (bench run 2026-10-18).
REAL_ERR_6 = """CMD[0] ver
1.0.4.25
-OK-
CMD[1] connect COM4
Connecting COM4 at 115200 OK
-OK-
CMD[2] loadfw --autodetect "D:\\mpcli/fw"
-ERR: 6-
disconnect -OK-
Task execution failure
recv hci evt Fail!
Detect FW Fail
"""


def process(return_code: int, stdout: str = "") -> ProcessResult:
    return ProcessResult(("-P", "x.bin"), ProcessOutcome.COMPLETED, return_code, stdout, "", 1.0)


@pytest.mark.unit
def test_table_matches_user_guide():
    assert sorted(MPCLI_ERROR_CODES) == list(range(19))
    assert MPCLI_ERROR_CODES[4].name == "ic type error"
    assert MPCLI_ERROR_CODES[6].name == "hci event error"
    assert MPCLI_ERROR_CODES[11].name == "Flash verify error"
    assert MPCLI_ERROR_CODES[18].name == "Dump data error"
    assert all(entry.meaning and entry.action for entry in MPCLI_ERROR_CODES.values())


@pytest.mark.unit
def test_failed_step_is_found_in_real_output():
    assert failed_step(REAL_ERR_6) == "loadfw"


@pytest.mark.unit
def test_describe_real_error_6():
    text = describe(6, REAL_ERR_6)
    assert text.startswith("MPCLI ERR 6 (hci event error) at loadfw:")
    assert "did not answer" in text and "download mode" in text


@pytest.mark.unit
def test_unknown_code_is_still_reported():
    assert "MPCLI ERR 42" in describe(42) and "unknown" in describe(42)


@pytest.mark.unit
def test_programming_failure_message_explains_code():
    result = parse_programming_result(process(6, REAL_ERR_6))
    assert result.message.startswith("MPCLI ERR 6 (hci event error) at loadfw")


@pytest.mark.unit
def test_readback_failure_message_explains_code():
    result = parse_mac_readback(process(9))
    assert result.message.startswith("Read-back failed: MPCLI ERR 9 (Timeout error)")
