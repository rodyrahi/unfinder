# /// script
# requires-python = ">=3.10"
# dependencies = ["PySide6>=6.6"]
# ///
"""Build the app icon (assets/unfinder.icns + assets/unfinder.png) from assets/unfinder.svg.

The artwork already has its own rounded shape (white frame around a dark square), so it is
used exactly as drawn: fitted into the 824×824 area of Apple's 1024×1024 app-icon grid, with
a soft shadow underneath like other macOS apps.

Run:  uv run make_icon.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "assets", "unfinder.svg")
PNG = os.path.join(HERE, "assets", "unfinder.png")
ICNS = os.path.join(HERE, "assets", "unfinder.icns")

CANVAS, BODY = 1024, 824   # Apple's macOS app-icon grid
CORNER = 29                 # corner radius of the artwork's outer frame, in SVG units


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / CANVAS, size / CANVAS)

    svg = QSvgRenderer(SVG)
    view = svg.viewBoxF()
    k = BODY / max(view.width(), view.height())          # keep the artwork's proportions
    w, h = view.width() * k, view.height() * k
    art = QRectF((CANVAS - w) / 2, (CANVAS - h) / 2, w, h)
    radius = CORNER * k

    # soft drop shadow: a few expanding, fading rounded rectangles below the icon
    for i in range(12, 0, -1):
        shadow = QPainterPath()
        shadow.addRoundedRect(art.adjusted(-i, -i + 10, i, i + 10), radius + i, radius + i)
        p.fillPath(shadow, QColor(0, 0, 0, 7))

    svg.render(p, art)
    p.end()
    return img


def main() -> None:
    QGuiApplication(sys.argv)
    render(1024).save(PNG)
    iconset = os.path.join(tempfile.mkdtemp(), "unfinder.iconset")
    os.makedirs(iconset)
    for pt in (16, 32, 128, 256, 512):
        render(pt).save(os.path.join(iconset, f"icon_{pt}x{pt}.png"))
        render(pt * 2).save(os.path.join(iconset, f"icon_{pt}x{pt}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", ICNS], check=True)
    shutil.rmtree(os.path.dirname(iconset))
    print(f"wrote {PNG} and {ICNS}")


if __name__ == "__main__":
    main()
