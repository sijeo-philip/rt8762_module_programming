"""Input field for a USB keyboard-wedge QR / badge scanner."""

from __future__ import annotations

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QLineEdit, QWidget

from stationapp.drivers.qr_scanner import ScanDebouncer, ScanFormat


class ScannerLineEdit(QLineEdit):
    """Normalises scanner keystrokes and emits one complete code.

    scanned(code)  a complete code; the field is cleared for the next scan
    rejected(raw)  Enter pressed on input that is not a complete code
    repeated(code) the same code read again within the debounce window;
                   discarded (not emitted as scanned)

    Fixed-length codes complete once the keystroke burst settles
    (settle_ms after the last character). Variable-length codes complete
    on Enter, or - when idle_complete is set - when the burst settles.
    """

    scanned = pyqtSignal(str)
    rejected = pyqtSignal(str)
    repeated = pyqtSignal(str)

    def __init__(self, scan_format: ScanFormat, *, settle_ms: int = 80, idle_complete: bool = False, debounce_ms: int = 2000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._format = scan_format
        self._debouncer = ScanDebouncer(debounce_ms / 1000.0)
        self._idle_complete = idle_complete
        self._normalising = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(settle_ms)
        self._timer.timeout.connect(self._settled)
        self.textChanged.connect(self._on_text_changed)
        self.returnPressed.connect(self._on_return)

    @property
    def scan_format(self) -> ScanFormat:
        return self._format

    def _on_text_changed(self, text: str) -> None:
        if self._normalising:
            return
        cleaned = self._format.normalise(text)
        if cleaned != text:
            self._normalising = True
            self.setText(cleaned)
            self._normalising = False
        if cleaned:
            self._timer.start()

    def _settled(self) -> None:
        code = self.text()
        if not self._format.is_complete(code):
            return
        if self._format.length or self._idle_complete:
            self._emit(code)

    def _on_return(self) -> None:
        self._timer.stop()
        raw = self.text()
        if not raw:
            return  # e.g. the Enter suffix after an already-completed scan
        code = self._format.normalise(raw)
        if self._format.is_complete(code):
            self._emit(code)
        else:
            self.clear()
            self.rejected.emit(raw)

    def submit(self, raw: str) -> None:
        """A complete read from a scanner that does not type (HID POS mode).

        Same normalisation and repeat-read filter as a typed scan.
        """
        self._timer.stop()
        code = self._format.normalise(raw)
        if self._format.is_complete(code):
            self._emit(code)
        else:
            self.rejected.emit(raw)

    def forget_last_scan(self) -> None:
        """The last scan was refused: let an immediate re-scan through."""
        self._debouncer.forget()

    def _emit(self, code: str) -> None:
        self._timer.stop()
        self._normalising = True
        self.clear()
        self._normalising = False
        if not self._debouncer.accept(code):
            self.repeated.emit(code)
            return
        self.scanned.emit(code)
