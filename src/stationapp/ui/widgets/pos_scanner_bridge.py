"""Qt side of the HID POS scanner: reads arrive as GUI-thread signals."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from stationapp.drivers.pos_scanner import PosScannerService


class PosScannerBridge(QObject):
    """scanned(text): one read; status_changed(text): connection state.

    The service calls back on its own thread; emitting a signal there
    delivers it to receivers on the GUI thread (queued connection).
    """

    scanned = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None, *, service_factory=PosScannerService) -> None:
        super().__init__(parent)
        self._service = service_factory(on_scan=self.scanned.emit, on_status=self.status_changed.emit)

    @property
    def status(self) -> str:
        return self._service.status

    def start(self) -> None:
        self._service.start()

    def stop(self) -> None:
        self._service.stop()
