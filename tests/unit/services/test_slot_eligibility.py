from stationapp.services.serial_topology import (
    ResolvedSlot,
    SerialTopology,
    SlotTopologyStatus,
)
from stationapp.services.slot_eligibility import (
    SlotEligibilityService,
    SlotEligibilityStatus,
)

from stationapp.drivers.serial.types import (
    DiscoveredSerialPort,
    UsbSerialIdentity,
)

import pytest

def make_slot(slot_number: int, status: SlotTopologyStatus, *, com_port: str | None = None) -> ResolvedSlot:
    port = None
    if com_port is not None:
        port = DiscoveredSerialPort(
            device=com_port,
            identity=UsbSerialIdentity(
                vid=0x10C4,
                pid=0xEA60,
                serial_number=(
                    f"PORT{slot_number:03d}"
                ),
                location=(
                    f"1-3.{slot_number}"
                ),
            ),
        )

    return ResolvedSlot(slot_number=slot_number, status=status, port=port, message=status.value)

def test_ready_slot_is_eligible():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.READY,
                com_port="COM7",
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    slot = eligibility.get(1)

    assert (
        slot.status
        is SlotEligibilityStatus.ELIGIBLE
    )

    assert slot.eligible is True
    assert slot.com_port == "COM7"

def test_missing_slot_is_blocked():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.MISSING,
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    slot = eligibility.get(1)

    assert (
        slot.status
        is SlotEligibilityStatus.BLOCKED_MISSING
    )

    assert slot.eligible is False
    assert slot.com_port is None

def test_moved_slot_is_blocked():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.MOVED,
                com_port="COM7",
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    slot = eligibility.get(1)

    assert (
        slot.status
        is SlotEligibilityStatus.BLOCKED_MOVED
    )

    assert slot.eligible is False

def test_unbound_slot_is_blocked():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.UNBOUND,
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    slot = eligibility.get(1)

    assert (
        slot.status
        is SlotEligibilityStatus.BLOCKED_UNBOUND
    )

def test_one_bad_slot_does_not_block_good_slots():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.READY,
                com_port="COM7",
            ),
            make_slot(
                2,
                SlotTopologyStatus.READY,
                com_port="COM8",
            ),
            make_slot(
                3,
                SlotTopologyStatus.MISSING,
            ),
            make_slot(
                4,
                SlotTopologyStatus.READY,
                com_port="COM10",
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    assert (
        eligibility.eligible_slot_numbers
        == (1, 2, 4)
    )

    assert eligibility.eligible_count == 3
    assert eligibility.blocked_count == 1

def test_require_com_port_returns_only_for_eligible_slot():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.READY,
                com_port="COM7",
            ),
            make_slot(
                2,
                SlotTopologyStatus.MISSING,
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    assert (
        eligibility.require_com_port(1)
        == "COM7"
    )

    with pytest.raises(RuntimeError):
        eligibility.require_com_port(2)


def test_operation_targets_include_only_eligible_slots():

    topology = SerialTopology(
        slots=(
            make_slot(
                1,
                SlotTopologyStatus.READY,
                com_port="COM7",
            ),
            make_slot(
                2,
                SlotTopologyStatus.MISSING,
            ),
            make_slot(
                3,
                SlotTopologyStatus.READY,
                com_port="COM9",
            ),
        )
    )

    eligibility = (
        SlotEligibilityService()
        .evaluate(topology)
    )

    targets = (
        eligibility.operation_targets
    )

    assert [
        (
            target.slot_number,
            target.com_port,
        )
        for target in targets
    ] == [
        (1, "COM7"),
        (3, "COM9"),
    ]