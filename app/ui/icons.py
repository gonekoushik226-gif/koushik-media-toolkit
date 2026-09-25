"""SVG icons from assets/icons, tinted to the current theme."""

from __future__ import annotations

import logging
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.config.paths import asset_path

log = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _svg_text(name: str) -> str:
    try:
        return asset_path("icons", f"{name}.svg").read_text(encoding="utf-8")
    except OSError:
        log.warning("Icon %s is missing", name)
        return ""


def pixmap(name: str, color: str, size: int = 32, ratio: float = 2.0) -> QPixmap:
    svg = _svg_text(name).replace("currentColor", color)
    result = QPixmap(QSize(int(size * ratio), int(size * ratio)))
    result.fill(Qt.GlobalColor.transparent)
    if svg:
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
        painter.end()
    result.setDevicePixelRatio(ratio)
    return result


def icon(name: str, color: str, size: int = 24) -> QIcon:
    return QIcon(pixmap(name, color, size))
