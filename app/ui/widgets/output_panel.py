"""Output folder + file name + "open folder when done" (used by every task)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from app.core.errors import InvalidInputError
from app.utils.filenames import build_filename, sanitize_filename, strip_known_extension
from app.utils.system import open_path


class OutputPanel(QGroupBox):
    """``multi=True`` asks for a base name instead of a file name (for tasks
    that create several files, such as Split PDF)."""

    def __init__(self, ctx, title: str = "Save as", multi: bool = False, name_label: str | None = None, parent=None):
        super().__init__(title, parent)
        self.ctx = ctx
        self._multi = multi
        self._user_named = False
        self._user_folder = False
        self._ext = ""

        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Output folder")
        self.folder.textEdited.connect(lambda _: setattr(self, "_user_folder", True))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        open_button = QPushButton("Open")
        open_button.setToolTip("Open this folder in Explorer")
        open_button.clicked.connect(self._open_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(open_button)

        self.name = QLineEdit()
        self.name.textEdited.connect(lambda _: setattr(self, "_user_named", True))
        self._ext_label = QLabel("")
        self._ext_label.setObjectName("muted")
        name_row = QHBoxLayout()
        name_row.addWidget(self.name, 1)
        name_row.addWidget(self._ext_label)

        self.open_when_done = QCheckBox("Open the folder when finished")
        self.open_when_done.setChecked(ctx.settings.open_folder_after)

        form = QFormLayout(self)
        form.addRow("Folder:", self._wrap(folder_row))
        form.addRow(name_label or ("Base name:" if multi else "File name:"), self._wrap(name_row))
        form.addRow("", self.open_when_done)

    @staticmethod
    def _wrap(layout) -> QWidget:
        widget = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        return widget

    # -- defaults ----------------------------------------------------------
    def suggest_name(self, stem: str, force: bool = False) -> None:
        if force or not self._user_named or not self.name.text().strip():
            self.name.setText(sanitize_filename(stem))
            self._user_named = False

    def suggest_folder(self, folder: Path | None, force: bool = False) -> None:
        if folder is None:
            return
        if force or not self._user_folder or not self.folder.text().strip():
            self.folder.setText(str(folder))
            self._user_folder = False

    def set_extension(self, ext: str | None) -> None:
        self._ext = (ext or "").lstrip(".").lower()
        self._ext_label.setText(f".{self._ext}" if self._ext else "(extension chosen automatically)")

    def extension(self) -> str:
        return self._ext

    # -- results -----------------------------------------------------------
    def folder_path(self) -> Path:
        text = self.folder.text().strip().strip('"')
        if not text:
            raise InvalidInputError("Please choose an output folder.")
        folder = Path(text)
        if folder.exists() and not folder.is_dir():
            raise InvalidInputError(f"The output folder is not a folder:\n{folder}")
        if not folder.is_absolute():
            raise InvalidInputError("Please choose a complete folder path (for example C:\\Users\\You\\Videos).")
        return folder

    def base_name(self) -> str:
        text = self.name.text().strip()
        if not text:
            raise InvalidInputError("Please enter a file name.")
        return sanitize_filename(strip_known_extension(text, self._ext))

    def output_path(self) -> Path:
        """Full output path (not checked for conflicts - see confirm_output)."""
        return self.folder_path() / build_filename(self.base_name(), self._ext)

    def reveal_when_done(self) -> bool:
        return self.open_when_done.isChecked()

    # -- actions -----------------------------------------------------------
    def _browse(self) -> None:
        start = self.folder.text().strip() or self.ctx.start_dir()
        folder = QFileDialog.getExistingDirectory(self, "Choose the output folder", start)
        if folder:
            self.folder.setText(str(Path(folder)))
            self._user_folder = True
            self.ctx.remember_dir(folder)

    def _open_folder(self) -> None:
        text = self.folder.text().strip()
        if text and Path(text).is_dir():
            open_path(Path(text))
