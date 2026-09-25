"""Choose one input file (browse button or drag & drop)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget


class _DropLineEdit(QLineEdit):
    dropped = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            event.acceptProposedAction()
            self.dropped.emit(Path(urls[0].toLocalFile()))


class FilePicker(QWidget):
    changed = Signal(object)  # Path | None

    def __init__(self, ctx, extensions: Iterable[str], dialog_filter: str, placeholder: str = "Choose a file...",
                 parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
        self._filter = dialog_filter
        self._path: Path | None = None
        self.edit = _DropLineEdit()
        self.edit.setPlaceholderText(f"{placeholder}  (or drag a file here)")
        self.edit.dropped.connect(self.set_path)
        self.edit.editingFinished.connect(self._typed)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

    def path(self) -> Path | None:
        return self._path if self._path and self._path.is_file() else None

    def set_path(self, path: Path | None) -> None:
        if path is not None:
            path = Path(path)
            if self._extensions and path.suffix.lower() not in self._extensions:
                from app.ui.dialogs import show_info

                show_info(self, "Unsupported file", f"'{path.name}' is not a supported file type for this task.")
                return
        changed = path != self._path
        self._path = path
        self.edit.setText(str(path) if path else "")
        if changed:
            self.changed.emit(self.path())

    def _typed(self) -> None:
        text = self.edit.text().strip().strip('"')
        if not text:
            self.set_path(None)
        elif Path(text) != self._path:
            self.set_path(Path(text))

    def _browse(self) -> None:
        start = self._path.parent if self._path else None
        file, _ = QFileDialog.getOpenFileName(self, "Choose a file", self.ctx.start_dir(start), self._filter)
        if file:
            self.ctx.remember_dir(file)
            self.set_path(Path(file))
