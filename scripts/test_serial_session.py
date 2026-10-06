"""Manual smoke test for one resolved jig serial port."""

from __future__ import annotations

import argparse

from stationapp.drivers.serial.session import (
    SerialSession,
    SerialSessionConfig,
)


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument("port", help="COM port, e.g. COM7")
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()
    config = SerialSessionConfig(port=args.port, baudrate=args.baud)
    print(f"Opening {config.port} at {config.baudrate} baud...")

    with SerialSession(config=config) as session:

        print("Serial port opened successfully.")
        print("Any currently buffered input:")
        print(session.read_available())
    print("Serial port closed cleanly.")


if __name__ == "__main__":
    main()