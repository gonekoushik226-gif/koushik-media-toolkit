"""An ordered list of input files with add/remove/reorder/sort controls."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.common import combo
from app.utils.sorting import SortKey, move_items, natural_key, shuffled, sort_paths

log = logging.getLogger(__name__)
PATH_ROLE = Qt.ItemDataRole.UserRole


class DropListWidget(QListWidget):
    """List that supports internal drag-to-reorder and dropping files from Explorer."""

    files_dropped = Signal(list)
    order_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(event)
            self.order_changed.emit()


class FileListWidget(QWidget):
    changed = Signal()
    current_changed = Signal(object)  # Path | None
    folder_added = Signal(object)  # Path of a folder whose files were just added

    def __init__(
        self,
        ctx,
        extensions: Iterable[str],
        dialog_filter: str,
        noun: str = "file",
        allow_folder: bool = False,
        sort_keys: Iterable[SortKey] = (SortKey.NATURAL, SortKey.NAME, SortKey.MODIFIED, SortKey.CREATED, SortKey.SIZE),
        allow_shuffle: bool = False,
        date_taken: Callable[[Path], float | None] | None = None,
        empty_text: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.ctx = ctx
        self._extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
        self._filter = dialog_filter
        self._noun = noun
        self._date_taken = date_taken

        self.list = DropListWidget()
        self.list.files_dropped.connect(self._on_dropped)
        self.list.order_changed.connect(self.changed.emit)
        self.list.currentItemChanged.connect(lambda *_: self.current_changed.emit(self.current_path()))
        self.list.setMinimumHeight(160)

        add = QPushButton(f"Add {noun}s...")
        add.clicked.connect(self._browse_files)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_selected)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        up = QPushButton("Move up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Move down")
        down.clicked.connect(lambda: self._move(1))

        self._count = QLabel("")
        self._count.setObjectName("muted")
        top = QHBoxLayout()
        top.addWidget(add)
        if allow_folder:
            folder = QPushButton("Add folder...")
            folder.clicked.connect(self._browse_folder)
            top.addWidget(folder)
        top.addWidget(remove)
        top.addWidget(clear)
        top.addStretch(1)
        top.addWidget(self._count)

        self.sort_combo = combo({key: key.label for key in sort_keys})
        sort = QPushButton("Sort")
        sort.setToolTip("Sort the list using the selected order")
        sort.clicked.connect(self._sort)
        reverse = QPushButton("Reverse")
        reverse.clicked.connect(self._reverse)
        bottom = QHBoxLayout()
        bottom.addWidget(up)
        bottom.addWidget(down)
        bottom.addSpacing(16)
        bottom.addWidget(QLabel("Order:"))
        bottom.addWidget(self.sort_combo)
        bottom.addWidget(sort)
        bottom.addWidget(reverse)
        if allow_shuffle:
            shuffle = QPushButton("Shuffle")
            shuffle.clicked.connect(self._shuffle)
            bottom.addWidget(shuffle)
        bottom.addStretch(1)

        self._empty = QLabel(empty_text or f"Add {noun}s with the button above, or drag them here from Explorer.")
        self._empty.setObjectName("hint")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addWidget(self._empty)
        layout.addLayout(bottom)
        self._refresh()

    # -- data --------------------------------------------------------------
    def paths(self) -> list[Path]:
        return [self.list.item(i).data(PATH_ROLE) for i in range(self.list.count())]

    def selected_paths(self) -> list[Path]:
        return [item.data(PATH_ROLE) for item in self.list.selectedItems()]

    def current_path(self) -> Path | None:
        item = self.list.currentItem()
        return item.data(PATH_ROLE) if item else None

    def set_paths(self, paths: Iterable[Path], keep_selection: bool = True) -> None:
        current = self.current_path() if keep_selection else None
        self.list.clear()
        for path in paths:
            self.list.addItem(self._make_item(Path(path)))
        if current is not None:
            for row in range(self.list.count()):
                if self.list.item(row).data(PATH_ROLE) == current:
                    self.list.setCurrentRow(row)
                    break
        self._refresh()
        self.changed.emit()

    def add_paths(self, paths: Iterable[Path]) -> int:
        existing = {str(p).lower() for p in self.paths()}
        added, skipped = 0, 0
        for path in paths:
            path = Path(path)
            if path.suffix.lower() not in self._extensions or not path.is_file():
                skipped += 1
                continue
            if str(path).lower() in existing:
                continue
            self.list.addItem(self._make_item(path))
            existing.add(str(path).lower())
            added += 1
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self._refresh()
        if added:
            self.changed.emit()
        if skipped:
            from app.ui.dialogs import show_info

            show_info(self, "Some files were skipped",
                      f"{skipped} item(s) were not added because they are not supported {self._noun}s.")
        return added

    def replace_path(self, old: Path, new: Path) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(PATH_ROLE) == old:
                item.setData(PATH_ROLE, new)
                item.setText(new.name)
                item.setToolTip(str(new))
        self.changed.emit()

    def set_icon(self, path: Path, icon: QIcon) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(PATH_ROLE) == path:
                item.setIcon(icon)

    def _make_item(self, path: Path) -> QListWidgetItem:
        item = QListWidgetItem(path.name)
        item.setData(PATH_ROLE, path)
        item.setToolTip(str(path))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        return item

    def _refresh(self) -> None:
        count = self.list.count()
        self._count.setText(f"{count} {self._noun}{'s' if count != 1 else ''}")
        self._empty.setVisible(count == 0)

    # -- actions -----------------------------------------------------------
    def _browse_files(self) -> None:
        first = self.paths()[0].parent if self.paths() else None
        files, _ = QFileDialog.getOpenFileNames(self, f"Add {self._noun}s", self.ctx.start_dir(first), self._filter)
        if files:
            self.ctx.remember_dir(files[0])
            self.add_paths(Path(f) for f in files)

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, f"Add all {self._noun}s in a folder", self.ctx.start_dir())
        if not folder:
            return
        self.ctx.remember_dir(folder)
        self.add_folder(Path(folder))

    def add_folder(self, folder: Path) -> int:
        """Add every supported file in ``folder`` in natural order (2 before 10)
        and announce the folder via ``folder_added``. Returns how many were added."""
        folder = Path(folder)
        found = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in self._extensions),
            key=lambda p: natural_key(p.name),
        )
        if not found:
            from app.ui.dialogs import show_info

            show_info(self, "Nothing found", f"The folder does not contain any supported {self._noun}s.")
            return 0
        added = self.add_paths(found)
        self.folder_added.emit(folder)
        return added

    def _on_dropped(self, paths: list[Path]) -> None:
        files: list[Path] = []
        for path in paths:
            if path.is_dir():
                self.add_folder(path)
            else:
                files.append(path)
        if files:
            self.add_paths(files)

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._refresh()
        self.changed.emit()

    def clear(self) -> None:
        self.list.clear()
        self._refresh()
        self.changed.emit()

    def _move(self, offset: int) -> None:
        rows = sorted(self.list.row(item) for item in self.list.selectedItems())
        if not rows:
            return
        paths, new_rows = move_items(self.paths(), rows, offset)
        self.set_paths(paths, keep_selection=False)
        self.list.clearSelection()
        for row in new_rows:
            self.list.item(row).setSelected(True)
        if new_rows:
            self.list.setCurrentRow(new_rows[0], QItemSelectionModel.SelectionFlag.NoUpdate)

    def _sort(self) -> None:
        key = self.sort_combo.currentData()
        self.set_paths(sort_paths(self.paths(), key, date_taken=self._date_taken))

    def _reverse(self) -> None:
        self.set_paths(list(reversed(self.paths())))

    def _shuffle(self) -> None:
        self.set_paths(shuffled(self.paths()))
