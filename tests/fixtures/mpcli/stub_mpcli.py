"""Realistic MPCLI stub used by Lesson 7 integration tests.

The script accepts the subset of MPCLI arguments used by StationApp.

Behaviour is controlled through environment variables so production
command builders remain exactly the same as they are for real MPCLI.

Environment variables:

    STUB_MPCLI_MODE

        success
        failure
        timeout
        no_device
        readback
        ambiguous_readback

    STUB_MPCLI_READBACK_MAC

        e.g. AABBCCDDEE01

The stub intentionally behaves as an external process.
"""

from __future__ import annotations

import os
import sys
import time


def value_after(args: list[str], option: str) -> str | None:
    try:
        index = args.index(option)
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    return args[index + 1]


def main() -> int:

    args = sys.argv[1:]
    mode = os.getenv("STUB_MPCLI_MODE", "success").strip().lower()
    readback_mac = os.getenv("STUB_MPCLI_READBACK_MAC", "AABBCCDDEE01").strip().upper()

    # ----------------------------------------------------------
    # Version
    # ----------------------------------------------------------

    if "-V" in args:

        print("MPCLI Version 1.0.4.25")
        return 0

    # ----------------------------------------------------------
    # Simulated hang
    # ----------------------------------------------------------

    if mode == "timeout":
        print("Programming started...", flush=True)
        time.sleep(60)
        return 0

    # ----------------------------------------------------------
    # Simulated no DUT
    # ----------------------------------------------------------

    if mode == "no_device":
        print("ERROR: device not detected", file=sys.stderr)
        return 4

    # ----------------------------------------------------------
    # Simulated explicit failure
    # ----------------------------------------------------------

    if mode == "failure":

        print("Programming failed", file=sys.stderr)
        return 3

    # ----------------------------------------------------------
    # MAC read-back
    # ----------------------------------------------------------

    if "-I" in args:
        if mode == "ambiguous_readback":
            print("MAC: AABBCCDDEE01")
            print("BT MAC: AABBCCDDEE02")
            return 0
        print(f"BT Address: {readback_mac}")
        return 0

    # ----------------------------------------------------------
    # Programming
    # ----------------------------------------------------------

    if "-P" in args:
        image = value_after(args, "-P")
        port = value_after(args, "-c" )
        mac = value_after(args, "-x" )

        if image is None:
            print("ERROR: packet image missing", file=sys.stderr)
            return 5

        if port is None:
            print("ERROR: COM port missing", file=sys.stderr)
            return 6

        if mac is None:
            print("ERROR: MAC missing", file=sys.stderr)
            return 7

        print(f"Open {port}" )
        print(f"Programming image: {image}")
        print(f"MAC: {mac}")
        print("Programming completed")

        return 0

    # ----------------------------------------------------------
    # Reboot only
    # ----------------------------------------------------------

    if "-r" in args:

        print("Device rebooted")

        return 0

    print("ERROR: unsupported command",file=sys.stderr)
    return 9


if __name__ == "__main__":
    raise SystemExit(main())


    