"""Bench check of the Pricol functional test on one real DUT.

Usage:
    python scripts/test_pricol_functional.py --port COM7
    python scripts/test_pricol_functional.py --port COM7 --baud 115200 --test app

Press Enter, then reset the module: the script waits for the module's
start-up line ("### Uart demo interrupt read uart data ###"), sends the
same command the station sends, and prints the raw exchange and verdict.
--no-wait sends immediately instead (the old behaviour).
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
    parser.add_argument("--reset-wait", type=float, default=30.0, help="seconds to wait for the start-up line")
    parser.add_argument("--no-wait", action="store_true", help="send immediately, without waiting for the start-up line")
    parser.add_argument("--dtr", choices=("on", "off"), default="off", help="DTR level while the port is open (default off)")
    parser.add_argument("--rts", choices=("on", "off"), default="off", help="RTS level while the port is open (default off)")
    args = parser.parse_args()

    tester = PricolFunctionalTester(
        baudrate=args.baud,
        response_timeout_seconds=args.timeout,
        wait_for_start=not args.no_wait,
        reset_wait_seconds=args.reset_wait,
        dtr=args.dtr == "on",
        rts=args.rts == "on",
    )
    print(f"{args.port}: {args.baud} baud, 8 data bits, no parity, 1 stop bit, no flow control, "
          f"DTR {args.dtr}, RTS {args.rts}")
    tests = ("app", "dfu") if args.test == "both" else (args.test,)

    all_passed = True
    for name in tests:
        if args.no_wait:
            input(f"\nReset the module on {args.port}, then press Enter to send PRICOL{name.upper()}...")
        else:
            input(f"\nPress Enter, then reset the module on {args.port} (waiting up to {args.reset_wait:.0f}s for it to start)...")
            print("Listening - reset the module now.")
        run = tester.run_app_test if name == "app" else tester.run_dfu_test
        outcome = run(slot_number=1, com_port=args.port)
        print(f"Start-up: {outcome.start_message!r}")
        print(f"Command : {outcome.command}")
        print(f"Expected: {outcome.expected!r}")
        print(f"Received: {outcome.response!r}")
        for label, text in (("start-up", outcome.start_message), ("response", outcome.response)):
            raw = text.encode("ascii", errors="replace")
            if raw:
                print(f"  {label} bytes ({len(raw)}): {raw[:64].hex(' ')}{' ...' if len(raw) > 64 else ''}")
        print(f"Verdict : {'PASS' if outcome.passed else 'FAIL' if outcome.passed is False else 'UNVERIFIED'} - {outcome.detail} ({outcome.duration_seconds}s)")
        all_passed = all_passed and outcome.passed is True

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
