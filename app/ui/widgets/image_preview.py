"""Image preview with an optional crop-selection rectangle."""

from __future__ import annotations

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from app.ui import theme


def pil_to_qimage(img: Image.Image) -> QImage:
    """Convert a Pillow image to a QImage that owns its pixel data (safe to
    create on worker threads)."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimage = QImage(data, img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return qimage.copy()


class ImagePreview(QWidget):
    selection_changed = Signal(object)  # (left, top, right, bottom) fractions, or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 260)
        self._pixmap: QPixmap | None = None
        self._message = "No image selected"
        self._crop_mode = False
        self._anchor: QPointF | None = None
        self._selection: QRectF | None = None  # fractions of the image
        self.setMouseTracking(True)

    # -- content -------------------------------------------------------------
    def set_image(self, image: QImage | None, message: str = "") -> None:
        self._pixmap = QPixmap.fromImage(image) if image is not None and not image.isNull() else None
        self._message = message or ("" if self._pixmap else "No image selected")
        self.clear_selection()
        self.update()

    def set_message(self, message: str) -> None:
        self._pixmap = None
        self._message = message
        self.clear_selection()
        self.update()

    def set_crop_mode(self, enabled: bool) -> None:
        self._crop_mode = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
        if not enabled:
            self.clear_selection()

    def clear_selection(self) -> None:
        if self._selection is not None:
            self._selection = None
            self.selection_changed.emit(None)
        self._anchor = None
        self.update()

    def selection(self) -> tuple[float, float, float, float] | None:
        if self._selection is None:
            return None
        rect = self._selection
        return rect.left(), rect.top(), rect.right(), rect.bottom()

    def selection_pixels(self) -> tuple[int, int] | None:
        """Selected size in preview pixels (for display only)."""
        if self._selection is None or self._pixmap is None:
            return None
        return (round(self._selection.width() * self._pixmap.width()),
                round(self._selection.height() * self._pixmap.height()))

    # -- geometry --------------------------------------------------------------
    def _image_rect(self) -> QRectF:
        if self._pixmap is None:
            return QRectF()
        ratio = self._pixmap.devicePixelRatio() or 1.0
        pw, ph = self._pixmap.width() / ratio, self._pixmap.height() / ratio
        scale = min((self.width() - 16) / pw, (self.height() - 16) / ph, 4.0)
        scale = max(scale, 0.01)
        w, h = pw * scale, ph * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_fraction(self, point: QPointF) -> QPointF:
        rect = self._image_rect()
        x = min(max((point.x() - rect.left()) / rect.width(), 0.0), 1.0)
        y = min(max((point.y() - rect.top()) / rect.height(), 0.0), 1.0)
        return QPointF(x, y)

    # -- painting --------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        colors = theme.colors()
        painter.fillRect(self.rect(), QColor(colors["surface_alt"]))
        if self._pixmap is None:
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            return
        target = self._image_rect()
        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        if self._selection is not None:
            sel = QRectF(target.left() + self._selection.left() * target.width(),
                         target.top() + self._selection.top() * target.height(),
                         self._selection.width() * target.width(),
                         self._selection.height() * target.height())
            shade = QColor(0, 0, 0, 120)
            painter.fillRect(QRectF(target.left(), target.top(), target.width(), sel.top() - target.top()), shade)
            painter.fillRect(QRectF(target.left(), sel.bottom(), target.width(), target.bottom() - sel.bottom()), shade)
            painter.fillRect(QRectF(target.left(), sel.top(), sel.left() - target.left(), sel.height()), shade)
            painter.fillRect(QRectF(sel.right(), sel.top(), target.right() - sel.right(), sel.height()), shade)
            pen = QPen(QColor("#ffffff"), 1.5, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(sel)
        elif self._crop_mode:
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(self.rect().adjusted(0, 0, 0, -6), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                             "Drag on the picture to select the area to keep")

    # -- mouse -----------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802
        if self._crop_mode and self._pixmap is not None and event.button() == Qt.MouseButton.LeftButton:
            self._anchor = self._to_fraction(event.position())
            self._selection = None
            self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._anchor is not None:
            current = self._to_fraction(event.position())
            self._selection = QRectF(self._anchor, current).normalized()
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._anchor is None:
            return
        self._anchor = None
        pixels = self.selection_pixels()
        if self._selection is None or pixels is None or pixels[0] < 2 or pixels[1] < 2:
            self._selection = None
            self.selection_changed.emit(None)
        else:
            self.selection_changed.emit(self.selection())
        self.update()
