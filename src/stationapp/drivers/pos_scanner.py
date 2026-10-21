"""USB barcode / QR scanner in HID POS mode (Windows Point of Service).

A scanner in "HID POS" mode (e.g. Honeywell Xenon 1470g, listed in
Device Manager as "POS HID Barcode scanner") does not type keystrokes,
so a keyboard-wedge input field never sees its reads. Windows exposes it
through Windows.Devices.PointOfService instead; this module claims the
scanner there and hands every read to a callback as text.

    service = PosScannerService(on_scan=print, on_status=print)
    service.start()     # background thread; reconnects on unplug/replug
    ...
    service.stop()

Reads arrive on a background thread: Qt code must forward them to the GUI
thread (see stationapp.ui.widgets.pos_scanner_bridge).

Requires the winrt-* packages (Windows 10 1709 or newer). Without them the
service reports itself unavailable and keyboard-wedge scanning still works.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

RETRY_SECONDS = 3.0
CALL_TIMEOUT_SECONDS = 10.0


class PosScannerUnavailable(RuntimeError):
    """HID POS scanning is not possible on this PC (no WinRT support)."""


def decode_label(data: bytes) -> str:
    """Scanned bytes -> text, without the terminator some scanners add."""
    text = data.decode("utf-8", errors="replace")
    return text.strip("\r\n\x00 ")


def choose_scanner(devices: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """Pick a USB scanner from (name, device id) pairs.

    Windows also offers the laptop camera as a "Microsoft BarcodeScanner";
    that is never the station's scanner. USB HID scanners have device ids
    starting with \\\\?\\HID#.
    """
    candidates = [
        (name, device_id)
        for name, device_id in devices
        if "camera" not in name.lower() and not device_id.upper().startswith("POS#")
    ]
    hid = [item for item in candidates if item[1].upper().startswith("\\\\?\\HID#")]
    return (hid or candidates or [None])[0]


def _import_winrt():
    try:
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.pointofservice import BarcodeScanner, PosConnectionTypes
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise PosScannerUnavailable(
            "HID POS scanner support is not installed (winrt-Windows.Devices.PointOfService)"
        ) from exc
    return DeviceInformation, BarcodeScanner, PosConnectionTypes


async def _call(operation, label: str, timeout: float = CALL_TIMEOUT_SECONDS):
    """Wait for a WinRT async operation by polling its status.

    A stuck Windows call is cancelled after `timeout` instead of blocking
    the scanner thread forever (seen with network scanner discovery).
    """
    started = time.monotonic()
    while int(operation.status) == 0:              # Started
        if time.monotonic() - started > timeout:
            operation.cancel()
            raise OSError(f"{label} did not answer within {timeout:.0f} s")
        await asyncio.sleep(0.05)
    if int(operation.status) != 1:                 # 1 = Completed
        raise OSError(f"{label} failed (status {int(operation.status)})")
    return operation.get_results()


class PosScannerService:
    """Owns the claimed HID POS scanner on a background thread."""

    def __init__(self, *, on_scan: Callable[[str], None], on_status: Callable[[str], None] | None = None, retry_seconds: float = RETRY_SECONDS) -> None:
        self._on_scan = on_scan
        self._on_status = on_status or (lambda _text: None)
        self._retry = retry_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self.status = "HID POS scanner: starting"
        self.connected_name: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            _import_winrt()
        except PosScannerUnavailable as exc:
            self._set_status(f"HID POS scanner: not available ({exc})")
            return
        self._thread = threading.Thread(target=self._thread_main, name="pos-scanner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass  # loop already closed
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            logger.info(text)
            try:
                self._on_status(text)
            except Exception:  # noqa: BLE001 - a UI callback must not stop the scanner
                logger.exception("Scanner status callback failed")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001
            logger.exception("HID POS scanner thread stopped")
            self._set_status(f"HID POS scanner: stopped ({exc})")

    async def _sleep(self, seconds: float) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        DeviceInformation, BarcodeScanner, PosConnectionTypes = _import_winrt()
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        # Locally attached (USB) scanners only: the default selector also
        # searches for network/Bluetooth scanners, which can hang.
        selector = BarcodeScanner.get_device_selector_with_connection_types(PosConnectionTypes.LOCAL)

        async def present() -> list[tuple[str, str]]:
            devices = await _call(DeviceInformation.find_all_async_aqs_filter(selector), "Scanner search")
            return [(info.name, info.id) for info in devices]

        while not self._stop.is_set():
            try:
                choice = choose_scanner(await present())
            except OSError as exc:
                self._set_status(f"HID POS scanner: search failed ({exc}) - retrying")
                await self._sleep(self._retry)
                continue
            if choice is None:
                self.connected_name = None
                self._set_status("HID POS scanner: not connected (keyboard-mode scanners still work)")
                await self._sleep(self._retry)
                continue

            name, device_id = choice
            claimed = None
            try:
                scanner = await _call(BarcodeScanner.from_id_async(device_id), f"Opening {name}")
                claimed = await _call(scanner.claim_scanner_async(), f"Claiming {name}") if scanner is not None else None
                if claimed is None:
                    self._set_status(f"HID POS scanner: {name} is in use by another application")
                    await self._sleep(self._retry)
                    continue

                # Keep the scanner if another application asks for it.
                claimed.add_release_device_requested(lambda sender, _args: sender.retain_device())
                claimed.add_data_received(self._data_received)
                claimed.is_decode_data_enabled = True
                await _call(claimed.enable_async(), f"Enabling {name}")
                self.connected_name = name
                self._set_status(f"HID POS scanner: {name} connected")

                # Stay claimed while the scanner is plugged in.
                while not self._stop.is_set():
                    await self._sleep(self._retry)
                    if self._stop.is_set():
                        break
                    if device_id not in {item[1] for item in await present()}:
                        self._set_status(f"HID POS scanner: {name} unplugged - waiting for it")
                        break
            except OSError as exc:
                self._set_status(f"HID POS scanner: {name} error ({exc}) - retrying")
                await self._sleep(self._retry)
            finally:
                self.connected_name = None
                if claimed is not None:
                    try:
                        claimed.close()
                    except OSError:
                        pass

    def _data_received(self, _sender, args) -> None:
        try:
            report = args.report
            buffer = report.scan_data_label or report.scan_data
            if buffer is None or not buffer.length:
                return
            text = decode_label(bytes(memoryview(buffer)))
        except Exception:  # noqa: BLE001 - one bad read must not stop the scanner
            logger.exception("Could not read HID POS scan data")
            return
        if text:
            logger.info("HID POS scan: %r", text)
            try:
                self._on_scan(text)
            except Exception:  # noqa: BLE001
                logger.exception("Scan callback failed")
