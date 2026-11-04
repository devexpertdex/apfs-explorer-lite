"""Draw the application icon and save it as a multi-size .ico plus a 256 px .png.

    python scripts/make_icon.py

Output: src/apfs_explorer_lite/resources/app.ico and app.png. Deterministic: the
same Qt version draws the same pixels. Small sizes use a simpler design (drive
and arrow only) so they stay legible in the taskbar and title bar.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "src" / "apfs_explorer_lite" / "resources"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

BLUE_TOP, BLUE_BOTTOM = QColor("#3B82F6"), QColor("#1D4ED8")
WHITE, LED, SLOT = QColor("#FFFFFF"), QColor("#22C55E"), QColor("#93C5FD")
FOLDER_BACK, FOLDER_FRONT = QColor("#F59E0B"), QColor("#FBBF24")


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 256, size / 256)  # draw on a 256 x 256 canvas
    simple = size <= 32

    tile = QRectF(8, 8, 240, 240)
    gradient = QLinearGradient(tile.topLeft(), tile.bottomLeft())
    gradient.setColorAt(0, BLUE_TOP)
    gradient.setColorAt(1, BLUE_BOTTOM)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(gradient)
    p.drawRoundedRect(tile, 52, 52)

    # External SSD: white body, green activity light.
    drive = QRectF(40, 34, 176, 84) if not simple else QRectF(32, 36, 192, 96)
    p.setBrush(WHITE)
    p.drawRoundedRect(drive, 22, 22)
    p.setBrush(LED)
    p.drawEllipse(QPointF(drive.right() - 30, drive.center().y()), 12 if not simple else 16, 12 if not simple else 16)
    if not simple:
        p.setPen(QPen(SLOT, 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(drive.left() + 28, drive.center().y()), QPointF(drive.right() - 62, drive.center().y()))
        p.setPen(Qt.PenStyle.NoPen)

    if not simple:
        # Folder on this PC.
        back = QPainterPath()
        back.moveTo(58, 160)
        back.lineTo(104, 160)
        back.lineTo(118, 174)
        back.lineTo(198, 174)
        back.quadTo(206, 174, 206, 182)
        back.lineTo(206, 218)
        back.lineTo(50, 218)
        back.lineTo(50, 168)
        back.quadTo(50, 160, 58, 160)
        p.setBrush(FOLDER_BACK)
        p.drawPath(back)
        p.setBrush(FOLDER_FRONT)
        p.drawRoundedRect(QRectF(50, 184, 156, 40), 8, 8)

    # Arrow from the drive down to the folder (one-way copy).
    arrow = QPainterPath()
    if simple:
        x, top, bottom, shaft, head = 128, 140, 232, 26, 46
    else:
        x, top, bottom, shaft, head = 128, 112, 198, 20, 38
    arrow.moveTo(x - shaft / 2, top)
    arrow.lineTo(x + shaft / 2, top)
    arrow.lineTo(x + shaft / 2, bottom - head)
    arrow.lineTo(x + head, bottom - head)
    arrow.lineTo(x, bottom)
    arrow.lineTo(x - head, bottom - head)
    arrow.lineTo(x - shaft / 2, bottom - head)
    arrow.closeSubpath()
    p.setBrush(WHITE)
    p.setPen(QPen(BLUE_BOTTOM, 8 if not simple else 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(arrow)
    p.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path: Path, images: dict[int, bytes]) -> None:
    """ICO with PNG-compressed entries (supported by Windows Vista and later)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    directory, blobs = b"", b""
    for size, blob in sorted(images.items()):
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset + len(blobs))
        blobs += blob
    path.write_bytes(header + directory + blobs)


def main() -> int:
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    OUT.mkdir(parents=True, exist_ok=True)
    images = {size: png_bytes(draw(size)) for size in SIZES}
    write_ico(OUT / "app.ico", images)
    (OUT / "app.png").write_bytes(images[256])
    print(f"Wrote {OUT / 'app.ico'} ({len(SIZES)} sizes) and app.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
