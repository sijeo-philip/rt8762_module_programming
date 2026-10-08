from pathlib import Path

from stationapp.drivers.serial.types import (DiscoveredSerialPort, UsbSerialIdentity)
from stationapp.services.serial_topology import (SerialTopologyService, SlotTopologyStatus)
from stationapp.services.slot_binding import (SlotBindingService)
from stationapp.services.slot_eligibility import (SlotEligibilityService)
from stationapp.drivers.serial.session import (SerialSession, SerialSessionConfig)
from stationapp.drivers.serial.errors import (SerialOpenError)

class FakeSerialBackend:

    def __init__(self) -> None:
        self._is_open = False
        self.tx = bytearray()
        self.rx = bytearray()

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def in_waiting(self) -> int:
        return len(self.rx)

    def open(self) -> None:
        self._is_open = True

    def close(self) -> None:
        self._is_open = False

    def read(self, size: int = 1) -> bytes:
        if not self.rx:
            return b""

        count = min(size, len(self.rx))
        data = bytes(self.rx[:count])
        del self.rx[:count]
        return data

    def write(self, data: bytes) -> int:
        self.tx.extend(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.rx.clear()

    def reset_output_buffer(self) -> None:
        self.tx.clear()

class FailingOpenBackend(FakeSerialBackend):

    def open(self) -> None:
        raise OSError(
            "Simulated UART failure"
        )



def make_port(slot_number: int, *, device: str | None = None, location: str | None = None) -> DiscoveredSerialPort:
    serial_number = f"PORT{slot_number:03d}"
    if device is None:
        device = f"COM{slot_number + 6}"

    if location is None:
        location = f"1-3.{slot_number}"

    return DiscoveredSerialPort(
        device=device,
        identity=UsbSerialIdentity(
            vid=0x10C4,
            pid=0xEA60,
            serial_number=serial_number,
            location=location,
        ),
        description="Simulated USB UART",
        manufacturer="Test Manufacturer",
        product="USB UART",
    )

def commission_eight_slots(binding_file: Path) -> SlotBindingService:

    service = SlotBindingService(binding_file=binding_file, station_id="STATION-01", jig_id="JIG-01", jig_positions=8)

    for slot_number in range(1, 9):
        service.bind(slot_number=slot_number, port=make_port(slot_number))
    return service


def test_all_eight_slots_resolve_and_are_eligible(tmp_path):

    binding_service = commission_eight_slots(tmp_path / "serial_bindings.json")
    bindings = binding_service.load()
    discovered = tuple(make_port(slot_number) for slot_number in range(1, 9))

    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)

    eligibility = SlotEligibilityService().evaluate(topology)

    assert topology.all_ready is True
    assert topology.ready_count == 8
    assert eligibility.eligible_count == 8
    assert eligibility.blocked_count == 0
    assert (eligibility.eligible_slot_numbers == (1, 2, 3, 4, 5, 6, 7, 8))

def test_all_slots_survive_com_renumbering(tmp_path):

    binding_service = commission_eight_slots(tmp_path / "serial_bindings.json")
    bindings = binding_service.load()
    discovered = tuple(make_port(slot_number, device=f"COM{40 + slot_number}",) for slot_number in range(1, 9))
    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)
    eligibility = SlotEligibilityService().evaluate(topology)
    assert topology.all_ready is True
    assert eligibility.require_com_port(1) == "COM41"
    assert eligibility.require_com_port(8) == "COM48"

def test_one_missing_slot_does_not_block_other_slots(tmp_path):

    binding_service = commission_eight_slots(tmp_path / "serial_bindings.json")
    bindings = binding_service.load()
    discovered = tuple(make_port(slot_number) for slot_number in range(1, 9) if slot_number != 3)
    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)
    eligibility = SlotEligibilityService().evaluate(topology)
    assert (topology.resolve_slot(3).status is SlotTopologyStatus.MISSING)
    assert (eligibility.eligible_slot_numbers == (1, 2, 4, 5, 6, 7, 8))
    assert eligibility.eligible_count == 7
    assert eligibility.blocked_count == 1 

def test_moved_adapter_blocks_only_that_slot(tmp_path):

    binding_service = commission_eight_slots(tmp_path / "serial_bindings.json")
    bindings = binding_service.load()
    discovered_ports = []
    for slot_number in range(1, 9):
        if slot_number == 5:
            discovered_ports.append(
                make_port(
                    5,
                    location="1-9.9",
                )
            )
        else:
            discovered_ports.append(make_port(slot_number))

    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=tuple(discovered_ports),
    )
    eligibility = SlotEligibilityService().evaluate(topology)
    assert (topology.resolve_slot(5).status is SlotTopologyStatus.MOVED)
    assert (eligibility.eligible_slot_numbers == (1, 2, 3, 4, 6, 7, 8))

def test_unbound_slot_is_excluded(tmp_path):
    binding_service = SlotBindingService(
        binding_file=(tmp_path / "serial_bindings.json"),
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
    )

    for slot_number in range(1, 9):
        if slot_number == 7:
            continue
        binding_service.bind(slot_number=slot_number, port=make_port(slot_number))
    bindings = binding_service.load()
    discovered = tuple(make_port(slot_number) for slot_number in range(1, 9))

    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)
    eligibility = SlotEligibilityService().evaluate(topology)

    assert (topology.resolve_slot(7).status is SlotTopologyStatus.UNBOUND)
    assert 7 not in (eligibility.eligible_slot_numbers)


def test_mixed_eight_slot_faults_are_isolated(tmp_path):

    binding_service = SlotBindingService(
        binding_file=(tmp_path / "serial_bindings.json"),
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
    )

    # Commission every slot except 7.
    for slot_number in range(1, 9):
        if slot_number == 7:
            continue

        binding_service.bind(slot_number=slot_number, port=make_port(slot_number))
    bindings = binding_service.load()

    discovered = (make_port(1), make_port(2),
         # Slot 3 deliberately missing.
        make_port(4),
        # Slot 5 exists but moved.
        make_port(5, location="1-8.5"),
        make_port(6),
        # Slot 7 is physically visible,
        # but it was never commissioned.
        make_port(7),
        make_port(8),
    )

    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)
    eligibility = SlotEligibilityService().evaluate(topology)

    assert (topology.resolve_slot(1).status is SlotTopologyStatus.READY)
    assert (topology.resolve_slot(3).status is SlotTopologyStatus.MISSING)
    assert (topology.resolve_slot(5).status is SlotTopologyStatus.MOVED)
    assert (topology.resolve_slot(7).status is SlotTopologyStatus.UNBOUND)
    assert (eligibility.eligible_slot_numbers == (1, 2, 4, 6, 8))
    assert eligibility.eligible_count == 5
    assert eligibility.blocked_count == 3


def test_serial_sessions_created_only_for_eligible_slots(tmp_path):

    binding_service = commission_eight_slots(tmp_path / "serial_bindings.json")
    bindings = binding_service.load()
    discovered = tuple(make_port(slot_number) for slot_number in range(1, 9) if slot_number != 3)
    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)
    eligibility = SlotEligibilityService().evaluate(topology)
    opened_slots = []
    for target in eligibility.operation_targets:
        backend = FakeSerialBackend()
        session = SerialSession(config=SerialSessionConfig(port=target.com_port), backend=backend)

        with session:
            opened_slots.append(target.slot_number)

    assert opened_slots == [1, 2, 4, 5, 6, 7, 8]


def test_one_serial_open_failure_can_be_isolated(tmp_path):

    binding_service = commission_eight_slots(tmp_path / "serial_bindings.json")
    bindings = binding_service.load()
    discovered = tuple(make_port(slot_number) for slot_number in range(1, 9))
    topology = SerialTopologyService().resolve(bindings=bindings, discovered_ports=discovered)
    eligibility = SlotEligibilityService().evaluate(topology)
    succeeded = []
    failed = []
    for target in eligibility.operation_targets:
        backend = (FailingOpenBackend() if target.slot_number == 4 else FakeSerialBackend())
        session = SerialSession(config=SerialSessionConfig(port=target.com_port), backend=backend)
        try:
            with session:
                succeeded.append(target.slot_number)

        except SerialOpenError:
            failed.append(target.slot_number)

    assert succeeded == [1, 2, 3, 5, 6, 7, 8]
    assert failed == [4]

def test_four_position_jig_supported(tmp_path):

    binding_service = SlotBindingService(
        binding_file=(
            tmp_path / "bindings.json"
        ),
        station_id="STATION-01",
        jig_id="JIG-4UP",
        jig_positions=4,
    )

    for slot_number in range(1, 5):

        binding_service.bind(
            slot_number=slot_number,
            port=make_port(slot_number),
        )

    bindings = binding_service.load()

    discovered = tuple(
        make_port(slot_number)
        for slot_number in range(1, 5)
    )

    topology = SerialTopologyService().resolve(
        bindings=bindings,
        discovered_ports=discovered,
    )

    eligibility = SlotEligibilityService().evaluate(
        topology
    )

    assert len(topology.slots) == 4
    assert eligibility.eligible_count == 4
