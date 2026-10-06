"""Display current jig serial topology."""

from __future__ import annotations

from stationapp.config import get_settings
from stationapp.services.serial_topology import (StationSerialTopologyService, SlotTopologyStatus)
from stationapp.services.slot_binding import (SlotBindingService)


def main() -> None:
    settings = get_settings()
    binding_service = SlotBindingService(
        binding_file=(settings.serial_binding_file),
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        jig_positions=settings.jig_positions,
    )

    topology_service = (StationSerialTopologyService(binding_service=binding_service))
    topology = topology_service.refresh()
    print()
    print(f"Station : {settings.station_id}")
    print(f"Jig     : {settings.jig_id}")
    print("=" * 100)
    print(
        f"{'Slot':<6}"
        f"{'Status':<12}"
        f"{'COM Port':<12}"
        f"{'USB Location':<20}"
        f"Stable Identity"
    )

    print("-" * 100)
    for slot in topology.slots:
        com_port = (
            slot.com_port or "-"
        )
        location = "-"
        if slot.port is not None:
            location = (slot.port.identity.location  or "-" )

        stable_key = (slot.stable_key or "-" )

        print(
            f"{slot.slot_number:<6}"
            f"{slot.status.value:<12}"
            f"{com_port:<12}"
            f"{location:<20}"
            f"{stable_key}"
        )

    print("-" * 100)

    print(
        f"Ready: "
        f"{topology.ready_count}/"
        f"{len(topology.slots)}"
    )

    if topology.all_ready:
        print("RESULT: JIG TOPOLOGY READY")
    else:
        print("RESULT: JIG TOPOLOGY NOT READY")
        print()
        for slot in topology.slots:
            if (slot.status is not SlotTopologyStatus.READY):
                print(
                    f"Slot {slot.slot_number}: "
                    f"{slot.message}"
                )


if __name__ == "__main__":
    main()