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
    InvalidDeviceTransition,
)


def reserved_mac(
    *,
    address: str,
    purpose: MacPurpose,
    slot_number: int = 1,
) -> AllocatedMac:
    record = AllocatedMac(
        allocation_id=f"ALLOC-{purpose.value}",
        address=MacAddress.parse(address),
        purpose=purpose,
    )
    record.reserve(
        batch_id="BATCH-001",
        slot_number=slot_number,
    )
    return record



@pytest.mark.unit
def test_stock_programming_and_rf_confirmation() -> None:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(stock.address)

    assert stock.status is MacStatus.ISSUED

    slot.verify_stock_rf("AA:BB:CC:00:00:01")

    assert stock.status is MacStatus.CONFIRMED
    assert slot.state is DeviceState.STOCK_RF_CONFIRMED


@pytest.mark.unit
def test_stock_rf_mismatch_holds_device_and_mac() -> None:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(stock.address)

    with pytest.raises(VerificationMismatch):
        slot.verify_stock_rf("AA:BB:CC:00:00:02")

    assert slot.state is DeviceState.HOLD

    # ISSUED moves to HOLD because identity was not confirmed.
    assert stock.status is MacStatus.HOLD


@pytest.mark.unit
def test_pricol_uses_different_mac_and_readback_only() -> None:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(stock.address)
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
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(stock.address)
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
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(stock.address)
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


@pytest.mark.unit
def test_qr_cannot_be_bound_before_functional_test_completes() -> None:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    with pytest.raises(InvalidDeviceTransition):
        slot.bind_module("MODULE-001")


def completed_slot() -> JigSlot:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )
    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)
    slot.verify_stock_readback(stock.address)
    slot.verify_stock_rf(stock.address)

    pricol = reserved_mac(
        address="DD:EE:FF:00:00:01",
        purpose=MacPurpose.PRICOL_PRODUCTION,
    )
    slot.assign_pricol_mac(pricol)
    slot.begin_pricol_programming()
    slot.complete_pricol_programming(True)
    slot.verify_pricol_readback(pricol.address)

    slot.record_pricol_app(True)
    slot.record_pricol_dfu(True)

    return slot


@pytest.mark.unit
def test_post_test_qr_binds_slot_and_both_macs() -> None:
    slot = completed_slot()

    assert slot.state is DeviceState.FUNCTIONAL_TEST_PASSED
    assert slot.module_qr is None

    slot.bind_module("MODULE-001")

    assert slot.state is DeviceState.QR_BOUND
    assert slot.module_qr == "MODULE-001"
    assert slot.stock_mac_record.module_qr == "MODULE-001"
    assert slot.pricol_mac_record.module_qr == "MODULE-001"


@pytest.mark.unit
def test_failed_device_still_receives_qr_genealogy() -> None:
    slot = completed_slot()

    # Recreate final result as a completed FAIL for this test.
    slot.state = DeviceState.PRICOL_MAC_CONFIRMED
    slot.functional.pricol_app_passed = False
    slot.functional.pricol_dfu_passed = None
    slot.record_pricol_dfu(True)

    assert slot.state is DeviceState.FUNCTIONAL_TEST_FAILED

    slot.bind_module("MODULE-FAILED-001")

    assert slot.state is DeviceState.QR_BOUND
    assert slot.module_qr == "MODULE-FAILED-001"

@pytest.mark.unit
def test_stock_rf_requires_mpcli_readback() -> None:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)

    with pytest.raises(InvalidDeviceTransition):
        slot.verify_stock_rf(stock.address)

    assert slot.state is DeviceState.STOCK_PROGRAMMED

    slot.verify_stock_readback(stock.address)

    assert (
        slot.state
        is DeviceState.STOCK_READBACK_VERIFIED
    )

    assert slot.stock_readback_mac == stock.address
    assert stock.status is MacStatus.ISSUED

    slot.verify_stock_rf(stock.address)

    assert slot.state is DeviceState.STOCK_RF_CONFIRMED
    assert stock.status is MacStatus.CONFIRMED

@pytest.mark.unit
def test_stock_readback_mismatch_holds_slot() -> None:
    slot = JigSlot(1)
    slot.bind_port("USB-SERIAL-001")

    stock = reserved_mac(
        address="AA:BB:CC:00:00:01",
        purpose=MacPurpose.STOCK_RF_TEST,
    )

    slot.assign_stock_mac(stock)
    slot.begin_stock_programming()
    slot.complete_stock_programming(True)

    with pytest.raises(VerificationMismatch):
        slot.verify_stock_readback(
            "AA:BB:CC:00:00:02"
        )

    assert slot.state is DeviceState.HOLD
    assert stock.status is MacStatus.HOLD

    assert slot.stock_readback_mac == MacAddress.parse(
        "AA:BB:CC:00:00:02"
    )

