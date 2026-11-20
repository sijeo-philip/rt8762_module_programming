"""Generate packaging/stationapp.ico (run once; the .ico is committed).

    .venv\\Scripts\\python packaging\\make_icon.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402


def main() -> int:
    _app = QGuiApplication(sys.argv)
    size = 256
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1f5fa8"))
    painter.drawRoundedRect(QRectF(8, 8, size - 16, size - 16), 44, 44)

    painter.setPen(QColor("white"))
    font = QFont("Segoe UI", 1, QFont.Weight.Bold)
    font.setPixelSize(118)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size - 30), Qt.AlignmentFlag.AlignCenter, "SA")

    font.setPixelSize(34)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(QRectF(0, 160, size, 60), Qt.AlignmentFlag.AlignCenter, "BLE")
    painter.end()

    target = Path(__file__).with_name("stationapp.ico")
    if not image.save(str(target), "ICO"):
        print("Could not write", target, file=sys.stderr)
        return 1
    print("Wrote", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
