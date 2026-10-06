"""Engineering utility for establishing physical jig serial bindings."""

from __future__ import annotations

from stationapp.config import get_settings
from stationapp.drivers.serial.discovery import (discover_stable_usb_ports)
from stationapp.services.slot_binding import (SerialIdentityAlreadyBoundError, SlotAlreadyBoundError, SlotBindingService)


def print_ports(ports) -> None:
    print()
    print("Available stable USB serial interfaces")
    print("=" * 90)

    for index, port in enumerate(ports, start=1):
        print(f"[{index}] {port.device:<8} {port.stable_key}")
        print(f"    Location : {port.identity.location or '-'}")
        print(f"    Product  : {port.product or port.description or '-'}")
    print()


def print_bindings(service: SlotBindingService) -> None:
    config = service.load()
    print()
    print(f"Station: {config.station_id} | Jig: {config.jig_id}")
    print("=" * 90)
    for slot_number in range(1, config.jig_positions + 1):
        binding = config.get(slot_number)
        if binding is None:
            print(f"Slot {slot_number}: UNBOUND")
        else:
            print(f"Slot {slot_number}: {binding.stable_key}")
    print()


def main() -> None:
    settings = get_settings()
    service = SlotBindingService(
        binding_file=(settings.serial_binding_file),
        station_id=settings.station_id,
        jig_id=settings.jig_id,
        jig_positions=settings.jig_positions,
    )

    while True:
        print_bindings(service)
        ports = discover_stable_usb_ports()
        if not ports:
            print("No stable USB serial interfaces were detected.")
            return

        print_ports(ports)
        answer = input("Enter physical slot number to bind (or Q to quit): ").strip()
        if answer.upper() == "Q":
            return

        try:
            slot_number = int(answer)
        except ValueError:
            print("Invalid slot number.")
            continue

        if not (1 <= slot_number <= settings.jig_positions):
            print(f"Slot must be 1..{settings.jig_positions}")
            continue

        selected_text = input("Select detected interface number: ").strip()

        try:
            selected_index = int(selected_text) - 1
            port = ports[selected_index]

        except (ValueError, IndexError):
            print("Invalid interface selection.")
            continue

        print()
        print(f"Physical slot : {slot_number}")
        print(f"Current port  : {port.device}")
        print(f"Stable ID     : {port.stable_key}")
        print(f"USB location  : {port.identity.location or '-'}")
        confirmation = input("Confirm binding? [y/N]: ").strip().lower()

        if confirmation != "y":
            print("Binding cancelled.")
            continue

        try:
            service.bind(
                slot_number=slot_number,
                port=port,
            )

        except SlotAlreadyBoundError as exc:
            print()
            print(exc)

            replace = input("Replace the existing slot binding? [y/N]: ").strip().lower()
            if replace != "y":
                continue

            try:
                service.bind(slot_number=slot_number, port=port, replace=True)
            except (
                SerialIdentityAlreadyBoundError,
                SlotAlreadyBoundError,
            ) as replace_exc:
                print(f"Binding failed: {replace_exc}")
                continue

        except SerialIdentityAlreadyBoundError as exc:
            print(f"Binding failed: {exc}")
            continue

        print(f"Slot {slot_number} binding saved.")


if __name__ == "__main__":
    main()