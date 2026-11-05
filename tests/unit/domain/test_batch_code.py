"""Batch Code rules (SDK 7.0 batch_id): entry, increment, exact split."""

from __future__ import annotations

import pytest

from stationapp.domain.batch_code import (
    BatchCodeError,
    BatchCodeState,
    next_batch_code,
    validate_batch_code,
    validate_threshold,
)


@pytest.mark.parametrize("code, following", [
    ("S5A01", "S5A02"),
    ("S5A09", "S5A10"),
    ("S5A99", "S5A100"),
    ("7", "8"),
    ("LOT-2026-0009", "LOT-2026-0010"),
    ("B 001", "B 002"),
])
def test_next_code_keeps_zero_padding(code, following):
    assert next_batch_code(code) == following


@pytest.mark.parametrize("text", ["", "   ", "S5A", "S5A01X", "S5Ä01", "S" * 63 + "\t1", "S" * 64 + "1"])
def test_bad_codes_are_refused(text):
    with pytest.raises(BatchCodeError):
        validate_batch_code(text)


def test_code_is_stripped_like_the_sdk():
    assert validate_batch_code("  S5A01 ") == "S5A01"


def test_next_code_never_exceeds_box_limit():
    with pytest.raises(BatchCodeError):
        next_batch_code("S" * 62 + "99")


@pytest.mark.parametrize("value", [0, -1, 1_000_001, "x", None])
def test_bad_threshold(value):
    with pytest.raises(BatchCodeError):
        validate_threshold(value)


def state(threshold=100, code="S5A01", passed=0):
    return BatchCodeState(session_id="S", threshold=threshold, code=code, passed=passed, series_start=code)


def test_exact_split_inside_a_tray():
    # 98 on S5A01; slots 1,2,3,5 pass, slot 4 fails.
    codes, after = state(passed=98).allocate([True, True, True, False, True])
    assert codes == ("S5A01", "S5A01", "S5A02", "S5A02", "S5A02")
    assert (after.code, after.passed) == ("S5A02", 2)


def test_code_moves_on_only_with_the_next_pass():
    codes, after = state(passed=99).allocate([True, False])
    # The 100th pass fills S5A01; the failure after it stays on S5A01.
    assert codes == ("S5A01", "S5A01")
    assert (after.code, after.passed, after.remaining) == ("S5A01", 100, 0)

    codes, after = after.allocate([False, True])
    assert codes == ("S5A01", "S5A02")
    assert (after.code, after.passed) == ("S5A02", 1)


def test_failures_are_not_counted():
    codes, after = state(passed=5).allocate([False] * 8)
    assert set(codes) == {"S5A01"} and after.passed == 5


def test_small_threshold_can_roll_twice_in_one_tray():
    codes, after = state(threshold=3, code="B09").allocate([True] * 8)
    assert codes == ("B09", "B09", "B09", "B10", "B10", "B10", "B11", "B11")
    assert (after.code, after.passed) == ("B11", 2)


def test_no_series_entered():
    with pytest.raises(BatchCodeError):
        BatchCodeState(session_id="S", threshold=10).allocate([True])


def test_new_series_restarts_count_and_round_trips():
    started = state(passed=40).started(" S6B01 ")
    assert (started.code, started.passed, started.series_start) == ("S6B01", 0, "S6B01")
    assert BatchCodeState.from_dict(started.to_dict()) == started
