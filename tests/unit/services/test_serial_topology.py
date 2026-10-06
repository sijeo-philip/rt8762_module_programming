import pytest

from stationapp.drivers.serial.types import (
    DiscoveredSerialPort,
    UsbSerialIdentity,
)
from stationapp.services.serial_topology import (
    SerialTopologyService,
    SlotTopologyStatus,
    SlotNotReadyError,
)
from stationapp.services.slot_binding import (
    SlotBinding,
    SlotBindingConfiguration,
)


def make_port(
    *,
    device: str,
    serial_number: str,
    location: str,
) -> DiscoveredSerialPort:

    return DiscoveredSerialPort(
        device=device,
        identity=UsbSerialIdentity(
            vid=0x10C4,
            pid=0xEA60,
            serial_number=serial_number,
            location=location,
        ),
        description="Test USB UART",
        manufacturer="Test Manufacturer",
        product="USB UART",
    )


def make_binding(
    *,
    slot_number: int,
    serial_number: str,
    location: str,
) -> SlotBinding:

    stable_key = (
        "USB:10C4:EA60:SERIAL:"
        f"{serial_number.upper()}"
    )

    return SlotBinding(
        slot_number=slot_number,
        stable_key=stable_key,
        vid=0x10C4,
        pid=0xEA60,
        serial_number=serial_number,
        location=location,
        description="Test USB UART",
        manufacturer="Test Manufacturer",
        product="USB UART",
    )


def make_config(
    *bindings: SlotBinding,
) -> SlotBindingConfiguration:

    return SlotBindingConfiguration(
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
        bindings=bindings,
    )


def test_bound_present_slot_is_ready():
    binding = make_binding(
        slot_number=1,
        serial_number="PORT001",
        location="1-3.1",
    )

    port = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(binding),
        discovered_ports=(port,),
    )

    slot = topology.resolve_slot(1)

    assert (
        slot.status
        is SlotTopologyStatus.READY
    )

    assert slot.ready is True
    assert slot.com_port == "COM7"


def test_windows_com_renumbering_does_not_break_slot():
    binding = make_binding(
        slot_number=1,
        serial_number="PORT001",
        location="1-3.1",
    )

    port = make_port(
        device="COM19",
        serial_number="PORT001",
        location="1-3.1",
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(binding),
        discovered_ports=(port,),
    )

    slot = topology.resolve_slot(1)

    assert slot.ready is True
    assert slot.com_port == "COM19"


def test_bound_but_missing_interface_is_reported():
    binding = make_binding(
        slot_number=1,
        serial_number="PORT001",
        location="1-3.1",
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(binding),
        discovered_ports=(),
    )

    slot = topology.resolve_slot(1)

    assert (
        slot.status
        is SlotTopologyStatus.MISSING
    )

    assert slot.com_port is None


def test_unbound_slot_is_reported():
    topology = SerialTopologyService().resolve(
        bindings=make_config(),
        discovered_ports=(),
    )

    slot = topology.resolve_slot(1)

    assert (
        slot.status
        is SlotTopologyStatus.UNBOUND
    )


def test_same_serial_at_different_location_is_moved():
    binding = make_binding(
        slot_number=1,
        serial_number="PORT001",
        location="1-3.1",
    )

    port = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.5",
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(binding),
        discovered_ports=(port,),
    )

    slot = topology.resolve_slot(1)

    assert (
        slot.status
        is SlotTopologyStatus.MOVED
    )

    assert slot.com_port == "COM7"


def test_duplicate_runtime_identity_is_rejected():
    first = make_port(
        device="COM7",
        serial_number="PORT001",
        location="1-3.1",
    )

    second = make_port(
        device="COM8",
        serial_number="PORT001",
        location="1-3.1",
    )

    with pytest.raises(
        ValueError,
        match="Duplicate runtime serial identity",
    ):
        SerialTopologyService().resolve(
            bindings=make_config(),
            discovered_ports=(
                first,
                second,
            ),
        )


def test_ready_count_is_reported():
    bindings = (
        make_binding(
            slot_number=1,
            serial_number="PORT001",
            location="1-3.1",
        ),
        make_binding(
            slot_number=2,
            serial_number="PORT002",
            location="1-3.2",
        ),
    )

    ports = (
        make_port(
            device="COM7",
            serial_number="PORT001",
            location="1-3.1",
        ),
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(*bindings),
        discovered_ports=ports,
    )

    assert topology.ready_count == 1

    # Eight-position jig:
    # one ready, seven not ready.
    assert topology.failed_count == 7

    assert topology.all_ready is False

def test_complete_eight_slot_jig_is_all_ready():
    bindings = []
    ports = []

    for slot_number in range(1, 9):

        serial = f"PORT{slot_number:03d}"

        location = (
            f"1-3.{slot_number}"
        )

        bindings.append(
            make_binding(
                slot_number=slot_number,
                serial_number=serial,
                location=location,
            )
        )

        ports.append(
            make_port(
                device=f"COM{slot_number + 5}",
                serial_number=serial,
                location=location,
            )
        )

    topology = SerialTopologyService().resolve(
        bindings=make_config(*bindings),
        discovered_ports=tuple(ports),
    )

    assert topology.ready_count == 8
    assert topology.failed_count == 0
    assert topology.all_ready is True

def test_require_com_port_returns_current_port():
    binding = make_binding(
        slot_number=1,
        serial_number="PORT001",
        location="1-3.1",
    )

    port = make_port(
        device="COM17",
        serial_number="PORT001",
        location="1-3.1",
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(binding),
        discovered_ports=(port,),
    )

    assert (
        topology.require_com_port(1)
        == "COM17"
    )

def test_require_com_port_rejects_missing_slot():
    binding = make_binding(
        slot_number=1,
        serial_number="PORT001",
        location="1-3.1",
    )

    topology = SerialTopologyService().resolve(
        bindings=make_config(binding),
        discovered_ports=(),
    )

    with pytest.raises(
        SlotNotReadyError
    ):
        topology.require_com_port(1)


