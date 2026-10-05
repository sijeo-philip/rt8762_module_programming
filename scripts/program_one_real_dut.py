from __future__ import annotations

import argparse
import os
from pathlib import Path

from stationapp.domain.mac import (MacAddress)
from stationapp.drivers.mpcli.configuration import (MpCliProgrammingProfile)
from stationapp.drivers.mpcli.driver import (MpCliDriver)
from stationapp.drivers.mpcli.process import (MpCliProcessRunner)
from stationapp.drivers.mpcli.types import (ProgrammingOutcome)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--mac", required=True)
    args = parser.parse_args()
    product_id = os.environ.get("STATION_MPCLI_PRODUCT_ID")
    secret_key = os.environ.get("STATION_MPCLI_SECRET_KEY")

    if not product_id:
        raise RuntimeError("STATION_MPCLI_PRODUCT_ID is not set")

    if not secret_key:
        raise RuntimeError("STATION_MPCLI_SECRET_KEY is not set")

    profile = MpCliProgrammingProfile(image_packet=args.image, product_id=product_id, secret_key=secret_key)
    runner = MpCliProcessRunner(args.exe)
    driver = MpCliDriver(runner)
    requested_mac = MacAddress.parse(args.mac)

    print("Programming requested MAC:", requested_mac.compact)
    result = driver.program(com_port=args.port, mac=requested_mac, profile=profile, timeout_seconds=60.0)
    print("Programming outcome:", result.outcome.value)
    print("Message:", result.message)
    print("Return code:", result.process.return_code)
    print("\nSTDOUT:")
    print(result.process.stdout)
    print("\nSTDERR:")
    print(result.process.stderr)

    if (result.outcome is not ProgrammingOutcome.SUCCESS):
        print("\nProgramming was not definite SUCCESS.")
        print("DO NOT reuse the requested MAC.")
        return 2

    print("\nNow performing read-back...")
    readback = driver.read_mac_structured(
        com_port=args.port,
        baud=profile.baud,
        timeout_seconds=10.0,
    )
    print("Readback outcome:", readback.outcome.value)
    print("Reported MAC:", (readback.mac.compact if readback.mac else None),)
    if readback.matches(requested_mac):
        print("\nPASS: programmed MAC matches read-back.")
        return 0
    print("\nFAIL: programmed MAC was not verified.")


    return 3


if __name__ == "__main__":
    raise SystemExit(
        main()
    )