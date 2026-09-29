from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stationapp.domain import (
    AllocationDocument,
    AllocationExpired,
    AllocationOwnershipMismatch,
    AllocationPurposeMismatch,
    DuplicateMacAddress,
    MacAddress,
    MacPurpose,
    MacStatus,
)


def make_document(
    *,
    purpose: MacPurpose = MacPurpose.PRICOL_PRODUCTION,
    expires_at: datetime | None = None,
) -> AllocationDocument:
    issued = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)

    return AllocationDocument(
        allocation_id="ALLOC-001",
        station_id="STN-01",
        jig_id="JIG-01",
        purpose=purpose,
        issued_at=issued,
        expires_at=expires_at,
        addresses=(
            MacAddress.parse("AA:BB:CC:00:00:01"),
            MacAddress.parse("AA:BB:CC:00:00:02"),
        ),
    )


@pytest.mark.unit
def test_valid_allocation_matches_station() -> None:
    document = make_document()

    document.validate_for_station(
        station_id="STN-01",
        jig_id="JIG-01",
        purpose=MacPurpose.PRICOL_PRODUCTION,
        now=datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc),
    )


@pytest.mark.unit
def test_allocation_for_another_station_is_rejected() -> None:
    document = make_document()

    with pytest.raises(AllocationOwnershipMismatch):
        document.validate_for_station(
            station_id="STN-02",
            jig_id="JIG-01",
            purpose=MacPurpose.PRICOL_PRODUCTION,
        )


@pytest.mark.unit
def test_allocation_for_wrong_purpose_is_rejected() -> None:
    document = make_document()

    with pytest.raises(AllocationPurposeMismatch):
        document.validate_for_station(
            station_id="STN-01",
            jig_id="JIG-01",
            purpose=MacPurpose.STOCK_RF_TEST,
        )


@pytest.mark.unit
def test_expired_allocation_is_rejected() -> None:
    expires = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    document = make_document(expires_at=expires)

    with pytest.raises(AllocationExpired):
        document.validate_for_station(
            station_id="STN-01",
            jig_id="JIG-01",
            purpose=MacPurpose.PRICOL_PRODUCTION,
            now=expires + timedelta(seconds=1),
        )


@pytest.mark.unit
def test_duplicate_addresses_in_document_are_rejected() -> None:
    duplicate = MacAddress.parse("AA:BB:CC:00:00:01")

    with pytest.raises(DuplicateMacAddress):
        AllocationDocument(
            allocation_id="ALLOC-001",
            station_id="STN-01",
            jig_id="JIG-01",
            purpose=MacPurpose.PRICOL_PRODUCTION,
            issued_at=datetime.now(timezone.utc),
            addresses=(duplicate, duplicate),
        )


@pytest.mark.unit
def test_document_creates_available_local_records() -> None:
    records = make_document().to_allocated_macs()

    assert len(records) == 2
    assert all(record.status is MacStatus.AVAILABLE for record in records)
    assert all(
        record.purpose is MacPurpose.PRICOL_PRODUCTION
        for record in records
    )
