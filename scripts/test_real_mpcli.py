from __future__ import annotations

import argparse
from pathlib import Path

from stationapp.drivers.mpcli.driver import (MpCliDriver)
from stationapp.drivers.mpcli.process import (MpCliProcessRunner)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)

    parser.add_argument("--port", required=True)
    args = parser.parse_args()
    runner = MpCliProcessRunner(args.exe)
    driver = MpCliDriver(runner)
    print("=== MPCLI VERSION ===")
    version = driver.version()
    print(version.stdout)
    if version.stderr:
        print("STDERR:", version.stderr)
    print("\n=== MAC READBACK ===")
    result = (driver.read_mac_structured(com_port=args.port, timeout_seconds=10.0))
    print("Outcome:", result.outcome.value)
    print("MAC:", (result.mac.compact if result.mac else None))
    print("Message:", result.message)
    print("\n=== RAW STDOUT ===")
    print(result.process.stdout)
    print("\n=== RAW STDERR ===")
    print(result.process.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


