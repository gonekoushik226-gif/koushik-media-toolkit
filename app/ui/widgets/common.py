"""Small layout helpers."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QPlainTextEdit, QPushButton, QWidget


def label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def hint(text: str) -> QLabel:
    return label(text, "hint")


def primary_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("primary", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(38)
    return button


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setStyleSheet("color: palette(mid);")
    return line


def combo(items: dict | list | tuple, current=None) -> QComboBox:
    """Combo box from {value: label} or a list of values."""
    box = QComboBox()
    pairs = items.items() if isinstance(items, dict) else [(item, str(item)) for item in items]
    for value, text in pairs:
        box.addItem(text, value)
    if current is not None:
        index = box.findData(current)
        if index >= 0:
            box.setCurrentIndex(index)
    return box


def monospace_view(min_height: int = 180) -> QPlainTextEdit:
    view = QPlainTextEdit()
    view.setReadOnly(True)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setStyleHint(QFont.StyleHint.Monospace)
    view.setFont(font)
    view.setMinimumHeight(min_height)
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return view


def set_bold(widget: QWidget, bold: bool = True) -> None:
    font = widget.font()
    font.setBold(bold)
    widget.setFont(font)
