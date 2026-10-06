from stationapp.drivers.serial.discovery import (discover_serial_ports)


def main() -> None:
    ports = discover_serial_ports()
    if not ports:
        print("No serial ports detected.")
        return

    print()
    print("Detected serial interfaces")
    print("=" * 90)

    for port in ports:
        print(f"Device       : {port.device}")
        print(
            "VID:PID      : "
            f"{port.identity.vid_hex or '-'}:"
            f"{port.identity.pid_hex or '-'}"
        )
        print(
            f"Serial       : "
            f"{port.identity.serial_number or '-'}"
        )
        print(
            f"Location     : "
            f"{port.identity.location or '-'}"
        )
        print(
            f"Manufacturer : "
            f"{port.manufacturer or '-'}"
        )
        print(
            f"Product      : "
            f"{port.product or '-'}"
        )
        try:
            key = port.stable_key
        except ValueError as exc:
            key = f"UNUSABLE: {exc}"
        print(f"Stable key   : {key}")
        print("-" * 90)


if __name__ == "__main__":
    main()