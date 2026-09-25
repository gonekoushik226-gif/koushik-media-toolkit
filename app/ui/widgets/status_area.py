"""The operation status area at the bottom of the window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from app.ui import theme
from app.utils.system import reveal_in_explorer


class StatusArea(QFrame):
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statusArea")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(12)

        self._icon = QLabel("●")
        self._icon.setFixedWidth(18)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_box = QVBoxLayout()
        text_box.setSpacing(0)
        self._title = QLabel("Ready")
        self._title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._detail = QLabel("")
        self._detail.setObjectName("muted")
        self._detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_box.addWidget(self._title)
        text_box.addWidget(self._detail)

        self._bar = QProgressBar()
        self._bar.setFixedWidth(280)
        self._bar.setRange(0, 1000)
        self._bar.setFormat("%p%")
        self._cancel = QPushButton("Cancel")
        self._cancel.clicked.connect(self.cancel_requested.emit)
        self._show = QPushButton("Show file")
        self._show.clicked.connect(self._reveal)

        layout.addWidget(self._icon)
        layout.addLayout(text_box, 1)
        layout.addWidget(self._bar)
        layout.addWidget(self._cancel)
        layout.addWidget(self._show)
        self._outputs: list[Path] = []
        self._running = False
        self.idle()

    # -- states ----------------------------------------------------------------
    def _set_icon(self, symbol: str, color_key: str) -> None:
        self._icon.setText(symbol)
        self._icon.setStyleSheet(f"color: {theme.colors()[color_key]}; font-size: 13pt; font-weight: 700;")

    def idle(self) -> None:
        self._running = False
        self._set_icon("●", "muted")
        self._title.setText("Ready")
        self._detail.setText("Choose a task. Progress of running tasks appears here.")
        self._bar.hide()
        self._cancel.hide()
        self._show.hide()

    def start(self, title: str) -> None:
        self._running = True
        self._outputs = []
        self._set_icon("◌", "accent")
        self._title.setText(title)
        self._detail.setText("Starting...")
        self._bar.show()
        self._bar.setRange(0, 0)  # busy until the first progress report
        self._cancel.show()
        self._cancel.setEnabled(True)
        self._cancel.setText("Cancel")
        self._show.hide()

    def set_status(self, text: str) -> None:
        if self._running and text:
            self._title.setText(text)

    def set_progress(self, fraction: object, detail: str) -> None:
        if not self._running:
            return
        if fraction is None:
            self._bar.setRange(0, 0)
        else:
            self._bar.setRange(0, 1000)
            self._bar.setValue(int(float(fraction) * 1000))
        if detail:
            self._detail.setText(detail)

    def set_cancelling(self) -> None:
        self._cancel.setEnabled(False)
        self._cancel.setText("Cancelling...")
        self._detail.setText("Stopping and cleaning up...")

    def finish_success(self, message: str, outputs: list[Path]) -> None:
        self._running = False
        self._outputs = [Path(p) for p in outputs]
        self._set_icon("✓", "success")
        self._title.setText("Done")
        self._detail.setText(message)
        self._bar.hide()
        self._cancel.hide()
        self._show.setVisible(bool(self._outputs))

    def finish_error(self, message: str) -> None:
        self._running = False
        self._set_icon("✗", "danger")
        self._title.setText("Failed")
        self._detail.setText(message)
        self._bar.hide()
        self._cancel.hide()
        self._show.hide()

    def finish_cancelled(self) -> None:
        self._running = False
        self._set_icon("■", "warning")
        self._title.setText("Cancelled")
        self._detail.setText("The operation was cancelled. No incomplete files were kept.")
        self._bar.hide()
        self._cancel.hide()
        self._show.hide()

    def _reveal(self) -> None:
        existing = [p for p in self._outputs if p.exists()]
        if existing:
            reveal_in_explorer(existing[0])
