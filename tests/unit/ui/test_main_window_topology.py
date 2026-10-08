from datetime import datetime, timezone

from stationapp.bootstrap import AppContext
from stationapp.config import Settings
from stationapp.drivers.serial.types import (
    DiscoveredSerialPort,
    UsbSerialIdentity,
)
from stationapp.services.serial_topology import (
    ResolvedSlot,
    SerialTopology,
    SlotTopologyStatus,
    SlotBindingService,
)
from stationapp.services.slot_binding import (
    SlotBinding,
)
from stationapp.ui.main_window import (
    MainWindow,
)

from stationapp.services.slot_eligibility import (
    SlotEligibilityService,
)

import pytest

from stationapp.services.slot_binding import (
    InvalidBindingFileError,
)

class FakeTopologyService:

    def __init__(
        self,
        topology: SerialTopology,
    ) -> None:
        self.topology = topology

    def refresh(
        self,
    ) -> SerialTopology:
        return self.topology


def make_ready_topology():

    port = DiscoveredSerialPort(
        device="COM7",
        identity=UsbSerialIdentity(
            vid=0x10C4,
            pid=0xEA60,
            serial_number="PORT001",
            location="1-3.1",
        ),
    )

    binding = SlotBinding(
        slot_number=1,
        stable_key=port.stable_key,
        vid=0x10C4,
        pid=0xEA60,
        serial_number="PORT001",
        location="1-3.1",
    )

    slot = ResolvedSlot(
        slot_number=1,
        status=SlotTopologyStatus.READY,
        binding=binding,
        port=port,
        message="Ready",
    )

    return SerialTopology(
        slots=(slot,)
    )

def test_topology_table_displays_slot(qtbot):

    settings = Settings(station_id="STATION-01", jig_id="JIG-01", jig_positions=8)
    context = AppContext(settings=settings, started_at=datetime.now(timezone.utc),
        serial_topology_service=(FakeTopologyService(make_ready_topology())), slot_eligibility_service=(SlotEligibilityService()),
    )
    window = MainWindow(context)
    qtbot.addWidget(window)
    window.refresh_serial_topology()
    assert (window._slot_table.rowCount() == 1)
    assert (window._slot_table.item(0, 0).text() == "1")
    assert (window._slot_table.item(0, 1).text() == "READY")
    assert (window._slot_table.item(0,2).text() == "ELIGIBLE")
    assert (window._slot_table.item(0, 3).text() == "COM7")



def test_corrupted_binding_file_is_not_silently_ignored(tmp_path):

    binding_file = (tmp_path / "serial_bindings.json")

    binding_file.write_text("{ invalid json", encoding="utf-8")

    service = SlotBindingService(
        binding_file=binding_file,
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
    )

    with pytest.raises(InvalidBindingFileError):
        service.load()


def test_empty_binding_file_means_uncommissioned_jig(tmp_path):

    binding_file = (tmp_path / "serial_bindings.json")

    binding_file.write_text("", encoding="utf-8")

    service = SlotBindingService(
        binding_file=binding_file,
        station_id="STATION-01",
        jig_id="JIG-01",
        jig_positions=8,
    )

    config = service.load()

    assert config.bindings == ()
    assert config.complete is False

