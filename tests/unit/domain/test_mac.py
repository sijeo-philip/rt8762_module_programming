from __future__ import annotations

import pytest

from stationapp.domain import (
    AllocatedMac,
    InvalidMacAddress,
    InvalidMacTransition,
    MacAddress,
    MacPurpose,
    MacStatus,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AABBCCDDEE01", "AA:BB:CC:DD:EE:01"),
        ("aa:bb:cc:dd:ee:01", "AA:BB:CC:DD:EE:01"),
        ("aa-bb-cc-dd-ee-01", "AA:BB:CC:DD:EE:01"),
        ("AA BB CC DD EE 01", "AA:BB:CC:DD:EE:01"),
        ("0xAABBCCDDEE01", "AA:BB:CC:DD:EE:01"),
    ],
)
def test_mac_parse_normalises_supported_formats(
    raw: str,
    expected: str,
) -> None:
    assert str(MacAddress.parse(raw)) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "AABB",
        "GG:BB:CC:DD:EE:01",
        "AA:BB:CC:DD:EE:01:02",
        -1,
        0x1000000000000,
        True,
    ],
)
def test_invalid_mac_is_rejected(raw) -> None:
    with pytest.raises(InvalidMacAddress):
        MacAddress.parse(raw)


@pytest.mark.unit
def test_authorised_mac_lifecycle() -> None:
    record = AllocatedMac(
        allocation_id="ALLOC-1",
        address=MacAddress.parse("AA:BB:CC:00:00:01"),
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    record.reserve(
        batch_id="BATCH-1",
        slot_number=1,
    )
    assert record.status is MacStatus.RESERVED

    record.begin_programming()
    assert record.status is MacStatus.PROGRAMMING

    record.mark_issued()
    assert record.status is MacStatus.ISSUED

    record.confirm()
    assert record.status is MacStatus.CONFIRMED


@pytest.mark.unit
def test_confirmed_mac_cannot_be_reused() -> None:
    record = AllocatedMac(
        allocation_id="ALLOC-1",
        address=MacAddress.parse("AA:BB:CC:00:00:01"),
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    record.reserve(
        batch_id="BATCH-1",
        slot_number=1,
    )
    record.begin_programming()
    record.mark_issued()
    record.confirm()

    with pytest.raises(InvalidMacTransition):
        record.reserve(
            batch_id="BATCH-2",
            slot_number=1,
        )


@pytest.mark.unit
def test_uncertain_programming_places_mac_on_hold() -> None:
    record = AllocatedMac(
        allocation_id="ALLOC-1",
        address=MacAddress.parse("AA:BB:CC:00:00:01"),
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    record.reserve(
        batch_id="BATCH-1",
        slot_number=1,
    )
    record.begin_programming()
    record.place_on_hold("PC lost power during MP CLI operation")

    assert record.status is MacStatus.HOLD
    assert "lost power" in record.hold_reason
    
    
@pytest.mark.unit
def test_module_qr_can_be_bound_after_mac_confirmation() -> None:
    record = AllocatedMac(
        allocation_id="ALLOC-1",
        address=MacAddress.parse("AA:BB:CC:00:00:01"),
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    record.reserve(
        batch_id="BATCH-1",
        slot_number=1,
    )
    record.begin_programming()
    record.mark_issued()
    record.confirm()

    assert record.module_qr is None

    record.bind_module_qr("MODULE-001")

    assert record.module_qr == "MODULE-001"

@pytest.mark.unit
def test_mac_qr_cannot_be_changed_after_binding() -> None:
    record = AllocatedMac(
        allocation_id="ALLOC-1",
        address=MacAddress.parse("AA:BB:CC:00:00:01"),
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    record.reserve(
        batch_id="BATCH-1",
        slot_number=1,
    )
    record.bind_module_qr("MODULE-001")

    with pytest.raises(InvalidMacTransition):
        record.bind_module_qr("MODULE-002")

