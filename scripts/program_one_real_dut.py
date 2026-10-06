from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from stationapp.domain.mac import (MacAddress)
from stationapp.drivers.mpcli import driver
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

    print("Requested MAC:", requested_mac.colon)
    print("\n=== STEP 1: FLASH FIRMWARE ===")
    flash = driver.flash(com_port=args.port, profile=profile, timeout_seconds=60.0)
    print("Flash command:", flash.command)
    time.sleep(2.0)
    print("Flash outcome:", flash.outcome.value)
    print("Return code:", flash.return_code)
    print("\nSTDOUT:")
    print(flash.stdout)
    print("\nSTDERR:")
    print(flash.stderr)
    if not flash.succeeded:
        print("\nFAIL: Firmware programming did not complete successfully.")
        return 2
    print("\nWaiting for DUT to settle after reset...")
    #time.sleep(2.0)
    print("\n=== STEP 2: WRITE MAC ===")
    mac_write = driver.set_mac(com_port=args.port, mac=requested_mac, profile=profile, timeout_seconds=15.0)
    print("MAC write command:", mac_write.command)
    print("MAC-write outcome:", mac_write.outcome.value)
    print("Return code:", mac_write.return_code)
    print("\nSTDOUT:")
    print(mac_write.stdout)
    print("\nSTDERR:")
    print(mac_write.stderr)
    if not mac_write.succeeded:
        print("\nFAIL: MAC programming did not complete successfully.")
        print("DO NOT reuse the requested MAC.")
        return 3
    #time.sleep(2.0)
    print("\n=== STEP 3: READ MAC BACK ===")

    readback = driver.read_mac_structured(com_port=args.port, baud=profile.baud, timeout_seconds=10.0)
    print("Readback outcome:", readback.outcome.value)

    print("Requested MAC:", requested_mac.colon)
    print("Reported MAC:", (readback.mac.colon if readback.mac else None),)

    if readback.matches(requested_mac):
        print("\nPASS: programmed MAC matches read-back.")
        return 0


    print("\nFAIL: programmed MAC was not verified.")

    return 4

if __name__ == "__main__":
    raise SystemExit(main())