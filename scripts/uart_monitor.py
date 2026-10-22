"""Raw UART monitor for troubleshooting a module's serial output.

Shows every byte the module sends after a reset, with timestamps, as hex
and as text, so "gibberish" can be told apart: wrong baud rate (bytes but
almost nothing readable), boot output at another speed followed by the
Pricol start-up line, or the DTR/RTS lines disturbing the module.

Port settings: 8 data bits, no parity, 1 stop bit, no flow control,
DTR and RTS off (as the station uses them; --dtr on / --rts on to compare).

Usage:
    # watch COM8 at 115200 for 15 s after you reset the module
    python scripts/uart_monitor.py --port COM8

    # compare: DTR and RTS on (pyserial's default, not used by the station)
    python scripts/uart_monitor.py --port COM8 --dtr on --rts on

    # try common baud rates one after another (reset the module each time)
    python scripts/uart_monitor.py --port COM8 --scan

    # after the start-up line, send PRICOLAPP<CR><LF> and show the reply
    python scripts/uart_monitor.py --port COM8 --send PRICOLAPP
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import serial

SCAN_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000)
START = re.compile(r"Uart demo interrupt read uart data", re.IGNORECASE)


def open_port(port: str, baud: int, dtr: str, rts: str) -> serial.Serial:
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = baud
    handle.bytesize = serial.EIGHTBITS
    handle.parity = serial.PARITY_NONE
    handle.stopbits = serial.STOPBITS_ONE
    handle.xonxoff = handle.rtscts = handle.dsrdtr = False
    handle.timeout = 0.05
    handle.dtr = dtr == "on"
    handle.rts = rts == "on"
    handle.open()
    handle.reset_input_buffer()
    return handle


def printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    good = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return good / len(data)


def show(offset: float, chunk: bytes) -> None:
    text = chunk.decode("ascii", errors="replace").replace("\r", "\\r").replace("\n", "\\n")
    print(f"  +{offset:6.3f}s  {len(chunk):3d} B  {chunk[:24].hex(' '):<71}  {text[:60]}")


def capture(port: str, baud: int, seconds: float, dtr: str, rts: str, *, prompt: bool, send: str | None) -> bytes:
    handle = open_port(port, baud, dtr, rts)
    print(f"\n{port}: {baud} baud 8N1, no flow control, DTR {dtr}, RTS {rts}")
    try:
        if prompt:
            input("Press Enter, then reset (power-cycle) the module...")
        print(f"Listening for {seconds:.0f} s - reset the module now.")
        received = bytearray()
        started = time.monotonic()
        sent = False
        while time.monotonic() - started < seconds:
            chunk = handle.read(4096)
            if chunk:
                received.extend(chunk)
                show(time.monotonic() - started, chunk)
            if send and not sent and START.search(received.decode("ascii", errors="replace")) and b"\n" in received[-2:] + b"\n":
                payload = send.encode("ascii") + b"\r\n"
                handle.write(payload)
                sent = True
                print(f"  >>> sent {payload!r} at +{time.monotonic() - started:.3f}s")
        return bytes(received)
    finally:
        handle.close()


def summarise(baud: int, data: bytes) -> tuple[float, bool]:
    ratio = printable_ratio(data)
    start = bool(START.search(data.decode("ascii", errors="replace")))
    verdict = (
        "no data" if not data
        else "readable" if ratio > 0.9
        else "partly readable (boot output at another speed, then text?)" if start
        else "mostly gibberish - probably the wrong baud rate"
    )
    print(f"  {baud} baud: {len(data)} bytes, {ratio:.0%} printable, start-up line {'FOUND' if start else 'not found'} -> {verdict}")
    return ratio, start


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw UART monitor (8N1, no flow control)")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", default="115200", help="baud rate, or several separated by commas")
    parser.add_argument("--scan", action="store_true", help=f"try {', '.join(map(str, SCAN_BAUDS))}")
    parser.add_argument("--seconds", type=float, default=15.0, help="listening time per baud rate")
    parser.add_argument("--dtr", choices=("on", "off"), default="off")
    parser.add_argument("--rts", choices=("on", "off"), default="off")
    parser.add_argument("--send", help="send this + CR LF once the start-up line has arrived (e.g. PRICOLAPP)")
    args = parser.parse_args()

    bauds = SCAN_BAUDS if args.scan else tuple(int(item) for item in args.baud.split(","))
    seconds = min(args.seconds, 6.0) if args.scan else args.seconds
    results = []
    try:
        for baud in bauds:
            data = capture(args.port, baud, seconds, args.dtr, args.rts, prompt=True, send=args.send)
            results.append((baud, *summarise(baud, data)))
    except serial.SerialException as exc:
        print(f"Cannot use {args.port}: {exc}  (is MPCLI, the bench or another terminal holding the port?)")
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")

    if len(results) > 1:
        print("\nSummary (best first):")
        for baud, ratio, start in sorted(results, key=lambda item: (item[2], item[1]), reverse=True):
            print(f"  {baud:>8} baud  {ratio:5.0%} printable  start-up line {'FOUND' if start else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
