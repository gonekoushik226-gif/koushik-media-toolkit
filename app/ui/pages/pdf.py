"""PDF module."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QButtonGroup, QGroupBox, QHBoxLayout, QRadioButton, QSpinBox, QVBoxLayout

from app.core.errors import AppError, InvalidInputError
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.services import pdf as pdf_service
from app.services.images import IMAGE_EXTENSIONS, image_date_taken
from app.ui.jobs import run_in_background
from app.ui.pages.base import InfoPanel, OperationPanel, OperationsPage
from app.ui.widgets.common import combo, hint, label
from app.ui.widgets.file_list import FileListWidget
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.output_panel import OutputPanel
from app.utils.sorting import SortKey

PDF_EXTS = (".pdf",)
PDF_FILTER = "PDF files (*.pdf);;All files (*.*)"
IMAGE_FILTER = "Images (" + " ".join(f"*{e}" for e in IMAGE_EXTENSIONS) + ");;All files (*.*)"
IMAGE_SORT_KEYS = (SortKey.NATURAL, SortKey.NAME, SortKey.DATE_TAKEN, SortKey.CREATED, SortKey.MODIFIED, SortKey.SIZE)


class CreatePdfPanel(OperationPanel):
    key = "create"
    title = "Create PDF from images"
    description = ("Combine images into one PDF - one image per page, in the order of the list. Photos are turned "
                   "the right way up automatically. Unmodified JPG and PNG images are embedded without quality loss.")
    action_text = "Create PDF"

    def build(self) -> None:
        self.files = FileListWidget(self.ctx, IMAGE_EXTENSIONS, IMAGE_FILTER, noun="image", allow_folder=True,
                                    sort_keys=IMAGE_SORT_KEYS, allow_shuffle=True, date_taken=image_date_taken)
        self.files.changed.connect(self._files_changed)
        self.body.addWidget(self.files, 1)
        _, form = self.form_group("Pages")
        self.page_size = combo(pdf_service.PAGE_SIZE_LABELS, "image")
        form.addRow("Page size:", self.page_size)
        self.output = OutputPanel(self.ctx)
        self.output.set_extension("pdf")
        self.output.suggest_name(self.ctx.settings.pdf_default_name or "combined")
        self.output.suggest_folder(self.ctx.output_dir_for(None))
        self.body.addWidget(self.output)

    def set_images(self, paths: list[Path]) -> None:
        self.files.set_paths(paths)

    def _files_changed(self) -> None:
        paths = self.files.paths()
        if paths:
            self.output.suggest_folder(self.ctx.output_dir_for(paths[0]))

    def run(self) -> None:
        paths = self.files.paths()
        if not paths:
            raise InvalidInputError("Please add at least one image.")
        page_size = self.page_size.currentData()
        output = self.confirm_output(self.output.output_path())
        if output is None:
            return
        sources = [pdf_service.ImageSource(p) for p in paths]
        self.start_job(f"Creating {output.name}", lambda ctx: pdf_service.images_to_pdf(sources, output, page_size, ctx),
                       reveal=self.output.reveal_when_done())


class MergePdfPanel(OperationPanel):
    key = "merge"
    title = "Merge PDFs"
    description = "Combine two or more PDF files into one. The order of the list is the order of the pages."
    action_text = "Merge"

    def build(self) -> None:
        self.files = FileListWidget(self.ctx, PDF_EXTS, PDF_FILTER, noun="PDF", allow_folder=True)
        self.files.changed.connect(self._files_changed)
        self.body.addWidget(self.files, 1)
        self.output = OutputPanel(self.ctx)
        self.output.set_extension("pdf")
        self.output.suggest_name("merged")
        self.output.suggest_folder(self.ctx.output_dir_for(None))
        self.body.addWidget(self.output)

    def _files_changed(self) -> None:
        paths = self.files.paths()
        if paths:
            self.output.suggest_folder(self.ctx.output_dir_for(paths[0]))

    def run(self) -> None:
        sources = self.files.paths()
        if len(sources) < 2:
            raise InvalidInputError("Please add at least two PDF files.")
        output = self.confirm_output(self.output.output_path())
        if output is None:
            return
        self.start_job(f"Merging {len(sources)} PDFs", lambda ctx: pdf_service.merge_pdfs(sources, output, ctx),
                       reveal=self.output.reveal_when_done())


class _SinglePdfPanel(OperationPanel):
    """Base for panels that work on one PDF and need its page count."""

    def build_input(self) -> None:
        group = QGroupBox("PDF file")
        box = QVBoxLayout(group)
        self.picker = FilePicker(self.ctx, PDF_EXTS, PDF_FILTER)
        self.picker.changed.connect(self._input_changed)
        self.page_info = label("", "muted")
        box.addWidget(self.picker)
        box.addWidget(self.page_info)
        self.body.addWidget(group)
        self.pages: int | None = None

    def _input_changed(self, path: Path | None) -> None:
        self.pages = None
        self.page_info.setText("")
        self.pages_changed()
        if path is None:
            return
        self.input_chosen(path)
        self.page_info.setText("Counting pages...")
        token = object()
        self._token = token

        def done(count: int) -> None:
            if self._token is token:
                self.pages = count
                self.page_info.setText(f"{count} page(s)")
                self.pages_changed()

        def failed(exc: BaseException) -> None:
            if self._token is token:
                self.page_info.setText(f"⚠ {exc.message if isinstance(exc, AppError) else exc}")

        run_in_background(lambda: pdf_service.count_pages(path), done, failed, owner=self)

    def input_chosen(self, path: Path) -> None: ...

    def pages_changed(self) -> None: ...

    def require_pdf(self) -> tuple[Path, int]:
        source = self.picker.path()
        if source is None:
            raise InvalidInputError("Please choose a PDF file first.")
        if self.pages is None:
            raise InvalidInputError("The PDF has not been read yet (or could not be read). Please wait or choose another file.")
        return source, self.pages


class SplitPdfPanel(_SinglePdfPanel):
    key = "split"
    title = "Split PDF"
    description = "Divide one PDF into several smaller PDF files."
    action_text = "Split"

    def build(self) -> None:
        self.build_input()
        group = QGroupBox("How to split")
        form = QVBoxLayout(group)
        self.modes = QButtonGroup(self)
        self.equal = QRadioButton("Into a number of equal parts")
        self.fixed = QRadioButton("Every N pages")
        self.equal.setChecked(True)
        self.modes.addButton(self.equal)
        self.modes.addButton(self.fixed)
        self.parts = QSpinBox()
        self.parts.setRange(1, 100000)
        self.parts.setValue(2)
        self.parts.setSuffix(" parts")
        self.per_file = QSpinBox()
        self.per_file.setRange(1, 100000)
        self.per_file.setValue(10)
        self.per_file.setSuffix(" pages per file")
        for radio, spin in ((self.equal, self.parts), (self.fixed, self.per_file)):
            row = QHBoxLayout()
            row.addWidget(radio)
            row.addWidget(spin)
            row.addStretch(1)
            form.addLayout(row)
            spin.valueChanged.connect(self.pages_changed)
        self.modes.buttonClicked.connect(lambda _: self.pages_changed())
        self.preview = label("", "")
        form.addWidget(self.preview)
        self.body.addWidget(group)
        self.output = OutputPanel(self.ctx, title="Save the parts", multi=True)
        self.output.set_extension("pdf")
        self.body.addWidget(self.output)
        self.naming = hint("")
        self.body.addWidget(self.naming)
        self.output.name.textChanged.connect(self._update_naming)
        self._update_naming()

    def _update_naming(self) -> None:
        base = self.output.name.text().strip() or "document"
        self.naming.setText(f"The files will be named {base}_part01.pdf, {base}_part02.pdf, ...")

    def input_chosen(self, path: Path) -> None:
        self.output.suggest_folder(self.ctx.output_dir_for(path))
        self.output.suggest_name(path.stem, force=True)

    def _ranges(self) -> list[tuple[int, int]]:
        if self.pages is None:
            raise InvalidInputError("The PDF has not been read yet.")
        if self.equal.isChecked():
            return pdf_service.compute_equal_parts(self.pages, self.parts.value())
        return pdf_service.compute_fixed_chunks(self.pages, self.per_file.value())

    def pages_changed(self) -> None:
        if not hasattr(self, "preview"):
            return
        if self.pages is None:
            self.preview.setText("Choose a PDF to see how it will be split.")
            return
        self.parts.setMaximum(max(1, self.pages))
        try:
            self.preview.setText("Result: " + pdf_service.describe_ranges(self._ranges()))
            self.preview.setObjectName("")
        except InvalidInputError as exc:
            self.preview.setText(exc.message)

    def run(self) -> None:
        source, _ = self.require_pdf()
        ranges = self._ranges()
        folder, base = self.output.folder_path(), self.output.base_name()
        self.start_job(f"Splitting {source.name}", lambda ctx: pdf_service.split_pdf(source, folder, base, ranges, ctx),
                       reveal=self.output.reveal_when_done())


class PdfToImagesPanel(_SinglePdfPanel):
    key = "to_images"
    title = "PDF to images"
    description = "Save every page of a PDF as a picture."
    action_text = "Convert"

    def build(self) -> None:
        self.build_input()
        _, form = self.form_group("Images")
        self.format = combo({"png": "PNG (sharp, larger files)", "jpg": "JPEG (smaller files)"}, "png")
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 600)
        self.dpi.setValue(self.ctx.settings.pdf_dpi)
        self.dpi.setSuffix(" DPI")
        form.addRow("Format:", self.format)
        form.addRow("Resolution:", self.dpi)
        form.addRow("", hint("150 DPI is good for reading on screen, 300 DPI for printing. Higher values make larger files."))
        self.output = OutputPanel(self.ctx, title="Save the images", multi=True, name_label="Name prefix:")
        self.body.addWidget(self.output)

    def input_chosen(self, path: Path) -> None:
        self.output.suggest_folder(self.ctx.output_dir_for(path) / f"{path.stem} pages")
        self.output.suggest_name(path.stem, force=True)

    def run(self) -> None:
        source, _ = self.require_pdf()
        folder, prefix = self.output.folder_path(), self.output.base_name()
        fmt, dpi = self.format.currentData(), self.dpi.value()
        self.start_job(f"Converting {source.name} to images",
                       lambda ctx: pdf_service.pdf_to_images(source, folder, prefix, fmt, dpi, ctx),
                       reveal=self.output.reveal_when_done())


class PdfInfoPanel(InfoPanel):
    key = "info"
    title = "PDF information"
    description = "See the number of pages, page size, title, author and other details of a PDF."
    input_extensions = PDF_EXTS
    input_filter = PDF_FILTER
    input_label = "PDF file"

    def describer(self):
        def describe(source: Path, ctx: JobContext) -> JobResult:
            ctx.set_status(f"Reading {source.name}...")
            ctx.set_progress(None)
            return JobResult(f"Information for {source.name}", details=pdf_service.pdf_summary(source))

        return describe


def build_pdf_page(ctx) -> OperationsPage:
    from app.ui.pages.downloads import PdfDownloadPanel

    operations = [
        ("create", "Create PDF from images", CreatePdfPanel),
        ("merge", "Merge PDFs", MergePdfPanel),
        ("split", "Split PDF", SplitPdfPanel),
        ("to_images", "PDF to images", PdfToImagesPanel),
        ("download", "Download PDF", PdfDownloadPanel),
        ("info", "PDF information", PdfInfoPanel),
    ]
    return OperationsPage(ctx, operations)

