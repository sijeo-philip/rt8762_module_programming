"""Bench check of the Pricol functional test on one real DUT.

Usage:
    python scripts/test_pricol_functional.py --port COM7
    python scripts/test_pricol_functional.py --port COM7 --baud 115200 --test app

Reset the module when prompted; the script then sends the same command
the station sends and prints the raw response and the verdict.
"""

from __future__ import annotations

import argparse

from stationapp.drivers.pricol.functional import PricolFunctionalTester


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--test", choices=("app", "dfu", "both"), default="both")
    args = parser.parse_args()

    tester = PricolFunctionalTester(baudrate=args.baud, response_timeout_seconds=args.timeout)
    tests = ("app", "dfu") if args.test == "both" else (args.test,)

    all_passed = True
    for name in tests:
        input(f"\nReset the module on {args.port}, then press Enter to send PRICOL{name.upper()}...")
        run = tester.run_app_test if name == "app" else tester.run_dfu_test
        outcome = run(slot_number=1, com_port=args.port)
        print(f"Command : {outcome.command}")
        print(f"Expected: {outcome.expected!r}")
        print(f"Received: {outcome.response!r}")
        print(f"Verdict : {'PASS' if outcome.passed else 'FAIL' if outcome.passed is False else 'UNVERIFIED'} - {outcome.detail} ({outcome.duration_seconds}s)")
        all_passed = all_passed and outcome.passed is True

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
