from __future__ import annotations

import pytest

from stationapp.domain import (
    AllocatedMac,
    DeviceState,
    JigSlot,
    MacAddress,
    MacPurpose,
    MacStatus,
    VerificationMismatch,
)


def reserved_mac(
    *,
    address: str,
    purpose: MacPurpose,
    module_qr: str = "MODULE-001",
    slot_number: int = 1,
) -> AllocatedMac:
    record = AllocatedMac(
        allocation_id=f"ALLOC-{purpose.value}",
        address=MacAddress.parse(address),
        purpose=purpose,
    )
    record.reserve(
        batch_id="BATCH-001",
        module_qr=module_qr,
        slot_number=slot_number,
    )
    return record


@pytest.mark.unit
def test_stock_programming_and_rf_confirmation() -> None:
    slot = JigSlot(1)
    slot.bind_module("MODULE-001")
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)

    assert stock.status is MacStatus.ISSUED

    slot.verify_stock_rf("AA:BB:CC:00:00:01")

    assert stock.status is MacStatus.CONFIRMED
    assert slot.state is DeviceState.STOCK_RF_CONFIRMED


@pytest.mark.unit
def test_stock_rf_mismatch_holds_device_and_mac() -> None:
    slot = JigSlot(1)
    slot.bind_module("MODULE-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)

    with pytest.raises(VerificationMismatch):
        slot.verify_stock_rf("AA:BB:CC:00:00:02")

    assert slot.state is DeviceState.HOLD

    # ISSUED moves to HOLD because identity was not confirmed.
    assert stock.status is MacStatus.HOLD


@pytest.mark.unit
def test_pricol_uses_different_mac_and_readback_only() -> None:
    slot = JigSlot(1)
    slot.bind_module("MODULE-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_rf(stock.address)

    pricol = reserved_mac(
        address="DD:EE:FF:00:00:01",
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )
    slot.assign_pricol_mac(pricol)
    slot.begin_pricol_programming()
    slot.complete_pricol_programming(True)
    slot.verify_pricol_readback(pricol.address)

    assert slot.stock_mac != slot.pricol_mac
    assert slot.pricol_readback_mac == slot.pricol_mac
    assert slot.state is DeviceState.PRICOL_MAC_CONFIRMED


@pytest.mark.unit
def test_stock_mac_cannot_be_reused_as_pricol_mac() -> None:
    slot = JigSlot(1)
    slot.bind_module("MODULE-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_rf(stock.address)

    pricol = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )

    with pytest.raises(VerificationMismatch):
        slot.assign_pricol_mac(pricol)

    assert slot.state is DeviceState.HOLD


@pytest.mark.unit
def test_pricol_readback_mismatch_holds_device() -> None:
    slot = JigSlot(1)
    slot.bind_module("MODULE-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_rf(stock.address)

    pricol = reserved_mac(
        address="DD:EE:FF:00:00:01",
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )
    slot.assign_pricol_mac(pricol)
    slot.begin_pricol_programming()
    slot.complete_pricol_programming(True)

    with pytest.raises(VerificationMismatch):
        slot.verify_pricol_readback("DD:EE:FF:00:00:02")

    assert slot.state is DeviceState.HOLD
    assert pricol.status is MacStatus.HOLD
