"""IMAGES module: list, preview, edit, convert, rename and make a PDF."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import AppError, InvalidInputError
from app.services import images as svc
from app.services import pdf as pdf_service
from app.ui.dialogs import confirm_output, show_error
from app.ui.jobs import run_in_background
from app.ui.pages.pdf import IMAGE_FILTER, IMAGE_SORT_KEYS
from app.ui.widgets.common import combo, hint, label, monospace_view, primary_button
from app.ui.widgets.file_list import FileListWidget
from app.ui.widgets.image_preview import ImagePreview, pil_to_qimage
from app.ui.widgets.output_panel import OutputPanel
from app.utils.system import open_path

log = logging.getLogger(__name__)
PREVIEW_SIZE = 1600
THUMB_SIZE = 72


def _make_thumbnail(path: Path):
    """Square thumbnail (centred on a transparent background) so names line up."""
    from PIL import Image

    loaded = svc.load_image(path, max_size=THUMB_SIZE * 2)
    square = Image.new("RGBA", (THUMB_SIZE * 2, THUMB_SIZE * 2), (0, 0, 0, 0))
    image = loaded.image.convert("RGBA")
    square.paste(image, ((square.width - image.width) // 2, (square.height - image.height) // 2))
    return pil_to_qimage(square)


class ImagesPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.edits: dict[Path, svc.ImageEdits] = {}
        self._previews: OrderedDict[Path, svc.LoadedImage] = OrderedDict()
        self._thumbs_requested: set[Path] = set()
        self._current: Path | None = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._render)

        self.files = FileListWidget(ctx, svc.IMAGE_EXTENSIONS, IMAGE_FILTER, noun="image", allow_folder=True,
                                    sort_keys=IMAGE_SORT_KEYS, allow_shuffle=True, date_taken=svc.image_date_taken)
        self.files.list.setIconSize(QPixmap(THUMB_SIZE, THUMB_SIZE).size())
        self.files.list.itemDoubleClicked.connect(lambda item: open_path(item.data(Qt.ItemDataRole.UserRole)))
        self.files.changed.connect(self._files_changed)
        self.files.current_changed.connect(self._current_changed)

        self.preview = ImagePreview()
        self.preview.selection_changed.connect(self._selection_changed)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(self.preview, 1)
        center_layout.addWidget(self.info)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._build_edit_tab()), "Edit")
        self.tabs.addTab(self._scroll(self._build_save_tab()), "Save / convert")
        self.tabs.addTab(self._scroll(self._build_pdf_tab()), "Make PDF")
        self.tabs.addTab(self._scroll(self._build_file_tab()), "File")
        self.tabs.setMinimumWidth(340)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.files)
        splitter.addWidget(center)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([340, 600, 360])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(splitter)
        self._update_controls()
        if ctx.jobs is not None:
            ctx.jobs.busy_changed.connect(self._on_busy)

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        widget.setObjectName("scrollContent")
        area.setWidget(widget)
        return area

    # ------------------------------------------------------------------ tabs --
    def _build_edit_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        scope_box = QGroupBox("Apply changes to")
        scope = QHBoxLayout(scope_box)
        self.scope_selected = QRadioButton("Selected image(s)")
        self.scope_all = QRadioButton("All images")
        self.scope_selected.setChecked(True)
        scope.addWidget(self.scope_selected)
        scope.addWidget(self.scope_all)
        layout.addWidget(scope_box)

        turn_box = QGroupBox("Rotate and flip")
        grid = QGridLayout(turn_box)
        buttons = [
            ("Rotate left", lambda: self._add_op(svc.rotate_op(270))),
            ("Rotate right", lambda: self._add_op(svc.rotate_op(90))),
            ("Rotate 180°", lambda: self._add_op(svc.rotate_op(180))),
            ("Flip horizontally", lambda: self._add_op(svc.flip_op("h"))),
            ("Flip vertically", lambda: self._add_op(svc.flip_op("v"))),
        ]
        for index, (text, action) in enumerate(buttons):
            button = QPushButton(text)
            button.clicked.connect(action)
            grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(turn_box)

        crop_box = QGroupBox("Crop")
        crop = QVBoxLayout(crop_box)
        row = QHBoxLayout()
        self.crop_toggle = QPushButton("Select area")
        self.crop_toggle.setCheckable(True)
        self.crop_toggle.toggled.connect(self.preview.set_crop_mode)
        self.crop_apply = QPushButton("Apply crop")
        self.crop_apply.setEnabled(False)
        self.crop_apply.clicked.connect(self._apply_crop)
        row.addWidget(self.crop_toggle)
        row.addWidget(self.crop_apply)
        crop.addLayout(row)
        self.crop_info = hint("Press 'Select area', then drag on the picture.")
        crop.addWidget(self.crop_info)
        layout.addWidget(crop_box)

        resize_box = QGroupBox("Resize")
        resize = QFormLayout(resize_box)
        self.resize_mode = combo({"percent": "Percentage", "fit": "Fit within (keep proportions)", "exact": "Exact size"},
                                 "percent")
        self.resize_mode.currentIndexChanged.connect(self._resize_mode_changed)
        self.resize_a = QSpinBox()
        self.resize_b = QSpinBox()
        for box in (self.resize_a, self.resize_b):
            box.setRange(1, 30000)
        self.resize_a.setValue(50)
        self.resize_b.setValue(1080)
        size_row = QHBoxLayout()
        size_row.addWidget(self.resize_a)
        self.resize_x = QLabel("x")
        size_row.addWidget(self.resize_x)
        size_row.addWidget(self.resize_b)
        size_row.addStretch(1)
        apply_resize = QPushButton("Apply resize")
        apply_resize.clicked.connect(self._apply_resize)
        resize.addRow("Mode:", self.resize_mode)
        resize.addRow("Size:", self._wrap(size_row))
        resize.addRow("", apply_resize)
        layout.addWidget(resize_box)
        self._resize_mode_changed()

        adjust_box = QGroupBox("Brightness and contrast")
        adjust = QFormLayout(adjust_box)
        self.brightness, self.brightness_label = self._slider(10, 300)
        self.contrast, self.contrast_label = self._slider(0, 300)
        adjust.addRow("Brightness:", self._slider_row(self.brightness, self.brightness_label))
        adjust.addRow("Contrast:", self._slider_row(self.contrast, self.contrast_label))
        self.brightness.valueChanged.connect(lambda v: self._set_adjustment("brightness", v))
        self.contrast.valueChanged.connect(lambda v: self._set_adjustment("contrast", v))
        layout.addWidget(adjust_box)

        changes_box = QGroupBox("Changes to this image")
        changes = QVBoxLayout(changes_box)
        self.changes = label("No changes", "muted")
        row = QHBoxLayout()
        undo = QPushButton("Undo last")
        undo.clicked.connect(self._undo)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        row.addWidget(undo)
        row.addWidget(reset)
        row.addStretch(1)
        changes.addWidget(self.changes)
        changes.addLayout(row)
        changes.addWidget(hint("Originals are never changed. Changes are applied when you save or make a PDF."))
        layout.addWidget(changes_box)
        layout.addStretch(1)
        return tab

    def _build_save_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        box = QGroupBox("Format")
        form = QFormLayout(box)
        self.save_format = combo(svc.OUTPUT_FORMATS, "keep")
        self.save_format.currentIndexChanged.connect(self._format_changed)
        self.quality, self.quality_label = self._slider(1, 100, 90, suffix="")
        self.keep_meta = QCheckBox("Keep photo information (EXIF: camera, date, ...)")
        self.keep_meta.setChecked(True)
        form.addRow("Save as:", self.save_format)
        form.addRow("Quality:", self._slider_row(self.quality, self.quality_label))
        form.addRow("", self.keep_meta)
        form.addRow("", hint("Quality applies to JPG and WEBP (higher = better, larger files)."))
        layout.addWidget(box)

        which = QGroupBox("Images to save")
        which_layout = QHBoxLayout(which)
        self.save_selected = QRadioButton("Selected image(s)")
        self.save_all = QRadioButton("All images")
        self.save_all.setChecked(True)
        group = QButtonGroup(tab)
        group.addButton(self.save_selected)
        group.addButton(self.save_all)
        which_layout.addWidget(self.save_selected)
        which_layout.addWidget(self.save_all)
        layout.addWidget(which)

        out = QGroupBox("Save to")
        out_form = QFormLayout(out)
        self.save_folder = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_save_folder)
        row = QHBoxLayout()
        row.addWidget(self.save_folder, 1)
        row.addWidget(browse)
        self.save_suffix = QLineEdit("_edited")
        self.save_open = QCheckBox("Open the folder when finished")
        self.save_open.setChecked(self.ctx.settings.open_folder_after)
        out_form.addRow("Folder:", self._wrap(row))
        out_form.addRow("Add to names:", self.save_suffix)
        out_form.addRow("", hint("photo.jpg becomes photo_edited.jpg. Leave empty to keep the names."))
        out_form.addRow("", self.save_open)
        layout.addWidget(out)
        self.save_button = primary_button("Save images")
        self.save_button.clicked.connect(lambda: self._guard(self._save))
        layout.addWidget(self.save_button)
        layout.addStretch(1)
        self._format_changed()
        return tab

    def _build_pdf_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(label("Combine all images in the list into one PDF, in list order, including your edits.",
                               "muted"))
        box = QGroupBox("Pages")
        form = QFormLayout(box)
        self.pdf_page_size = combo(pdf_service.PAGE_SIZE_LABELS, "image")
        form.addRow("Page size:", self.pdf_page_size)
        layout.addWidget(box)
        self.pdf_output = OutputPanel(self.ctx)
        self.pdf_output.set_extension("pdf")
        self.pdf_output.suggest_name(self.ctx.settings.pdf_default_name or "combined")
        layout.addWidget(self.pdf_output)
        self.pdf_button = primary_button("Create PDF")
        self.pdf_button.clicked.connect(lambda: self._guard(self._make_pdf))
        layout.addWidget(self.pdf_button)
        layout.addStretch(1)
        return tab

    def _build_file_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        box = QGroupBox("Rename the file on disk")
        form = QVBoxLayout(box)
        self.rename_edit = QLineEdit()
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(lambda: self._guard(self._rename))
        row = QHBoxLayout()
        row.addWidget(self.rename_edit, 1)
        row.addWidget(self.rename_button)
        form.addLayout(row)
        form.addWidget(hint("Renames the selected original file. The extension stays the same."))
        layout.addWidget(box)
        info_box = QGroupBox("Information")
        info = QVBoxLayout(info_box)
        self.details = monospace_view(160)
        info.addWidget(self.details)
        layout.addWidget(info_box)
        layout.addStretch(1)
        return tab

    # ------------------------------------------------------------- helpers --
    @staticmethod
    def _wrap(layout) -> QWidget:
        widget = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        return widget

    @staticmethod
    def _slider(low: int, high: int, value: int = 100, suffix: str = " %"):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        text = QLabel(f"{value}{suffix}")
        text.setMinimumWidth(48)
        slider.valueChanged.connect(lambda v: text.setText(f"{v}{suffix}"))
        return slider, text

    def _slider_row(self, slider: QSlider, text: QLabel) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(text)
        return self._wrap(row)

    def _guard(self, action) -> None:
        try:
            action()
        except AppError as exc:
            show_error(self.window(), exc)

    def _on_busy(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)
        self.pdf_button.setEnabled(not busy)

    def _targets(self) -> list[Path]:
        """Images affected by edit buttons."""
        if self.scope_all.isChecked():
            return self.files.paths()
        selected = self.files.selected_paths()
        if not selected and self._current is not None:
            selected = [self._current]
        return selected

    def _edits(self, path: Path) -> svc.ImageEdits:
        return self.edits.setdefault(path, svc.ImageEdits())

    # ------------------------------------------------------------- events --
    def _files_changed(self) -> None:
        paths = self.files.paths()
        for stale in [p for p in self.edits if p not in paths]:
            del self.edits[stale]
        for path in paths:
            if path not in self._thumbs_requested:
                self._thumbs_requested.add(path)
                run_in_background(lambda p=path: _make_thumbnail(p),
                                  lambda image, p=path: self.files.set_icon(p, QIcon(QPixmap.fromImage(image))),
                                  owner=self)
        if paths:
            folder = self.ctx.output_dir_for(paths[0])
            if not self.save_folder.text().strip():
                self.save_folder.setText(str(folder))
            self.pdf_output.suggest_folder(folder)
        if self._current not in paths:
            self._current_changed(self.files.current_path())
        self._update_controls()

    def _current_changed(self, path: Path | None) -> None:
        self._current = path
        self.crop_toggle.setChecked(False)
        self._sync_adjustments()
        self._update_controls()
        if path is None:
            self.preview.set_message("Add images with the buttons on the left, or drag them here from Explorer.")
            self.info.setText("")
            self.details.setPlainText("")
            self.rename_edit.clear()
            return
        self.rename_edit.setText(path.stem)
        self.details.setPlainText(svc.image_summary(path))
        if path in self._previews:
            self._render()
            return
        self.preview.set_message("Loading...")

        def loaded(result: svc.LoadedImage) -> None:
            self._previews[path] = result
            while len(self._previews) > 12:
                self._previews.popitem(last=False)
            if self._current == path:
                self._render()

        def failed(exc: BaseException) -> None:
            if self._current == path:
                message = exc.message if isinstance(exc, AppError) else str(exc)
                self.preview.set_message(f"Cannot show this image:\n{message}")

        run_in_background(lambda: svc.load_image(path, max_size=PREVIEW_SIZE), loaded, failed, owner=self)

    def _render(self) -> None:
        path = self._current
        loaded = self._previews.get(path) if path else None
        if loaded is None:
            return
        edits = self.edits.get(path, svc.ImageEdits())
        try:
            result = svc.apply_edits(loaded.image.copy(), edits, full_size=loaded.full_size)
        except Exception as exc:  # noqa: BLE001 - never crash the preview
            log.warning("Preview failed for %s", path, exc_info=True)
            self.preview.set_message(f"Preview failed: {exc}")
            return
        self.preview.set_image(pil_to_qimage(result))
        width, height = svc.size_after(loaded.full_size, edits.ops)
        size = f"{loaded.full_size[0]} x {loaded.full_size[1]}"
        if (width, height) != loaded.full_size:
            size += f"  →  {width} x {height} after changes"
        frames = f" · {loaded.frames} frames (only the first is used)" if loaded.frames > 1 else ""
        self.info.setText(f"{path.name} · {loaded.format or '?'} · {size}{frames}")
        self.changes.setText(edits.describe())

    def _update_controls(self) -> None:
        has_images = bool(self.files.paths())
        for widget in (self.tabs,):
            widget.setEnabled(has_images)

    def _sync_adjustments(self) -> None:
        edits = self.edits.get(self._current, svc.ImageEdits()) if self._current else svc.ImageEdits()
        for slider, value in ((self.brightness, edits.brightness), (self.contrast, edits.contrast)):
            slider.blockSignals(True)
            slider.setValue(round(value * 100))
            slider.blockSignals(False)
        self.brightness_label.setText(f"{self.brightness.value()} %")
        self.contrast_label.setText(f"{self.contrast.value()} %")
        self.changes.setText(edits.describe())

    # ------------------------------------------------------------- editing --
    def _add_op(self, op: svc.ImageOp) -> None:
        targets = self._targets()
        if not targets:
            return
        for path in targets:
            self._edits(path).ops.append(op)
        self._render()

    def _selection_changed(self, selection) -> None:
        self.crop_apply.setEnabled(selection is not None)
        if selection is None:
            self.crop_info.setText("Press 'Select area', then drag on the picture.")
            return
        loaded = self._previews.get(self._current) if self._current else None
        if loaded is not None:
            edits = self.edits.get(self._current, svc.ImageEdits())
            width, height = svc.size_after(loaded.full_size, edits.ops)
            left, top, right, bottom = selection
            self.crop_info.setText(f"Selected {round((right - left) * width)} x {round((bottom - top) * height)} pixels")

    def _apply_crop(self) -> None:
        selection = self.preview.selection()
        if selection is None:
            return
        try:
            op = svc.crop_op(*selection)
        except ValueError as exc:
            show_error(self.window(), InvalidInputError(str(exc)))
            return
        self._add_op(op)
        self.crop_toggle.setChecked(False)

    def _resize_mode_changed(self) -> None:
        percent = self.resize_mode.currentData() == "percent"
        self.resize_b.setVisible(not percent)
        self.resize_x.setVisible(not percent)
        self.resize_a.setSuffix(" %" if percent else " px")
        self.resize_b.setSuffix(" px")
        if percent:
            self.resize_a.setRange(1, 1000)
            self.resize_a.setValue(50)
        else:
            self.resize_a.setRange(1, 30000)
            self.resize_a.setValue(1920)

    def _apply_resize(self) -> None:
        mode = self.resize_mode.currentData()
        try:
            op = svc.resize_op(mode, self.resize_a.value(), None if mode == "percent" else self.resize_b.value())
        except ValueError as exc:
            show_error(self.window(), InvalidInputError(str(exc)))
            return
        self._add_op(op)

    def _set_adjustment(self, name: str, value: int) -> None:
        for path in self._targets():
            setattr(self._edits(path), name, value / 100)
        self._render_timer.start()

    def _undo(self) -> None:
        for path in self._targets():
            edits = self.edits.get(path)
            if edits and edits.ops:
                edits.ops.pop()
        self._render()

    def _reset(self) -> None:
        for path in self._targets():
            self.edits.pop(path, None)
        self._sync_adjustments()
        self._render()

    # ------------------------------------------------------------- saving --
    def _format_changed(self) -> None:
        self.quality.setEnabled(self.save_format.currentData() in ("jpg", "webp", "keep"))

    def _browse_save_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the output folder",
                                                  self.save_folder.text() or self.ctx.start_dir())
        if folder:
            self.save_folder.setText(str(Path(folder)))

    def _save(self) -> None:
        paths = self.files.paths() if self.save_all.isChecked() else (self.files.selected_paths() or
                                                                      ([self._current] if self._current else []))
        if not paths:
            raise InvalidInputError("There are no images to save.")
        folder_text = self.save_folder.text().strip()
        if not folder_text or not Path(folder_text).is_absolute():
            raise InvalidInputError("Please choose an output folder.")
        folder = Path(folder_text)
        tasks = [svc.ImageTask(path, self.edits.get(path, svc.ImageEdits()).copy()) for path in paths]
        fmt, quality = self.save_format.currentData(), self.quality.value()
        suffix, keep = self.save_suffix.text(), self.keep_meta.isChecked()
        self.ctx.jobs.start(f"Saving {len(tasks)} image(s)",
                            lambda ctx: svc.process_images(tasks, folder, fmt, quality, suffix, keep, ctx),
                            reveal=self.save_open.isChecked())

    def _make_pdf(self) -> None:
        paths = self.files.paths()
        if not paths:
            raise InvalidInputError("There are no images in the list.")
        output = confirm_output(self.window(), self.pdf_output.output_path(), self.ctx.settings.overwrite_policy)
        if output is None:
            return
        sources = [pdf_service.ImageSource(p, self.edits.get(p).copy() if p in self.edits else None) for p in paths]
        page_size = self.pdf_page_size.currentData()
        self.ctx.jobs.start(f"Creating {output.name}",
                            lambda ctx: pdf_service.images_to_pdf(sources, output, page_size, ctx),
                            reveal=self.pdf_output.reveal_when_done())

    def _rename(self) -> None:
        path = self._current
        if path is None:
            raise InvalidInputError("Please select an image first.")
        new_stem = self.rename_edit.text().strip()
        if not new_stem:
            raise InvalidInputError("Please enter a new name.")
        renamed = svc.rename_file(path, new_stem)
        if renamed == path:
            return
        if path in self.edits:
            self.edits[renamed] = self.edits.pop(path)
        if path in self._previews:
            self._previews[renamed] = self._previews.pop(path)
        self._thumbs_requested.add(renamed)
        self._current = renamed
        self.files.replace_path(path, renamed)
        self.rename_edit.setText(renamed.stem)
        self.details.setPlainText(svc.image_summary(renamed))


def build_images_page(ctx) -> ImagesPage:
    return ImagesPage(ctx)

