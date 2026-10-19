"""Regenerate the application icons in sqliteview/assets/icons/ (PNG, Windows .ico, macOS .icns).

Developer tool, needs Pillow:  python -m pip install pillow  &&  python scripts/make_icons.py
The generated files are committed, so building the app does not need Pillow.
"""
from __future__ import annotations

import io
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

from sqliteview.ui.icons import svg_markup  # noqa: E402

OUT = os.path.join(ROOT, "sqliteview", "assets", "icons")
ACCENT = "#4a63e7"


def render(size: int) -> Image.Image:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    margin = size * 0.06
    path = QPainterPath()
    path.addRoundedRect(QRectF(margin, margin, size - 2 * margin, size - 2 * margin), size * 0.22, size * 0.22)
    p.fillPath(path, QColor(ACCENT))
    glyph = QSvgRenderer(QByteArray(svg_markup("database", "#ffffff", 1.7).encode()))
    inset = size * 0.22
    glyph.render(p, QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def main() -> None:
    QGuiApplication([])
    os.makedirs(OUT, exist_ok=True)
    big = render(1024)
    big.save(os.path.join(OUT, "sqliteview-1024.png"))
    render(512).save(os.path.join(OUT, "sqliteview-512.png"))
    render(256).save(os.path.join(OUT, "sqliteview.png"))
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    render(256).save(os.path.join(OUT, "sqliteview.ico"), sizes=[(s, s) for s in ico_sizes])
    big.save(os.path.join(OUT, "sqliteview.icns"))
    print("Icons written to", OUT)


if __name__ == "__main__":
    main()
