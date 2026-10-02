from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stationapp.domain.operation_log import (
    OperationLevel,
    OperationLog,
)


def make_log(
    **overrides,
) -> OperationLog:

    values = {
        "operation_id": "OP-001",
        "station_id": "STATION-01",
        "level": OperationLevel.INFO,
        "category": "APPLICATION",
        "event_type": "APPLICATION_STARTED",
        "message": "Station application started",
        "occurred_at": datetime.now(
            timezone.utc
        ),
    }

    values.update(
        overrides
    )

    return OperationLog(
        **values
    )


def test_valid_operation_log() -> None:

    log = make_log()

    assert log.operation_id == "OP-001"


@pytest.mark.parametrize(
    "field_name",
    [
        "operation_id",
        "station_id",
        "category",
        "event_type",
        "message",
    ],
)
def test_required_text_fields_reject_empty(
    field_name: str,
) -> None:

    with pytest.raises(ValueError):
        make_log(
            **{
                field_name: "   "
            }
        )


def test_slot_number_must_be_positive() -> None:

    with pytest.raises(
        ValueError,
        match="slot_number",
    ):
        make_log(
            slot_number=0
        )


def test_occurred_at_must_be_timezone_aware() -> None:

    with pytest.raises(
        ValueError,
        match="timezone-aware",
    ):
        make_log(
            occurred_at=datetime(
                2026,
                10,
                1,
                12,
                0,
                0,
            )
        )


