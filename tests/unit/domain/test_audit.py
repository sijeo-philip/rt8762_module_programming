"""Unit tests for immutable audit records."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stationapp.domain.audit import AuditRecord


def make_record(
    **overrides,
) -> AuditRecord:

    values = {
        "audit_id": "AUDIT-001",
        "user_id": "USER-01",
        "station_id": "STATION-01",
        "record_type": "BATCH",
        "record_id": "BATCH-001",
        "action": "RELEASE_HOLD",
        "reason": "Quality approval",
        "created_at": datetime.now(
            timezone.utc
        ),
    }

    values.update(
        overrides
    )

    return AuditRecord(
        **values
    )


def test_valid_audit_record() -> None:

    record = make_record()

    assert record.audit_id == "AUDIT-001"


@pytest.mark.parametrize(
    "field_name",
    [
        "audit_id",
        "user_id",
        "station_id",
        "record_type",
        "record_id",
        "action",
        "reason",
    ],
)
def test_required_text_fields_cannot_be_empty(
    field_name: str,
) -> None:

    with pytest.raises(
        ValueError
    ):
        make_record(
            **{
                field_name: "   "
            }
        )


def test_timestamp_must_be_timezone_aware() -> None:

    with pytest.raises(
        ValueError,
        match="timezone-aware",
    ):
        make_record(
            created_at=datetime(
                2026,
                10,
                1,
                12,
                0,
                0,
            )
        )


