"""Building blocks for module pages.

* :class:`OperationPanel` - one task (title, description, options, Start button).
* :class:`SingleFilePanel` - a task with one input file and one output file;
  most video/audio tasks only override a few hooks.
* :class:`OperationsPage` - a module page: task list on the left, the chosen
  task's panel on the right.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import AppError, InvalidInputError
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.ui.dialogs import confirm_output, show_error
from app.ui.jobs import run_in_background
from app.ui.widgets.common import label, monospace_view, primary_button
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.output_panel import OutputPanel

log = logging.getLogger(__name__)


class OperationPanel(QWidget):
    key = ""
    title = ""
    description = ""
    action_text = "Start"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)
        heading = QLabel(self.title)
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        if self.description:
            outer.addWidget(label(self.description, "muted"))
        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        outer.addLayout(self.body)
        outer.addStretch(1)
        self.action_row = QHBoxLayout()
        self.action_row.addStretch(1)
        self.action_button = primary_button(self.action_text)
        self.action_button.clicked.connect(self._on_action)
        self.action_row.addWidget(self.action_button)
        outer.addLayout(self.action_row)
        self.build()
        if ctx.jobs is not None:
            ctx.jobs.busy_changed.connect(self._on_busy)

    # -- to override -------------------------------------------------------
    def build(self) -> None:
        """Add option widgets to ``self.body``."""

    def run(self) -> None:
        """Validate the options and start the job (raise AppError for bad input)."""

    # -- helpers -----------------------------------------------------------
    def _on_action(self) -> None:
        try:
            self.run()
        except AppError as exc:
            show_error(self.window(), exc)

    def _on_busy(self, busy: bool) -> None:
        self.action_button.setEnabled(not busy)

    def form_group(self, title: str) -> tuple[QGroupBox, QFormLayout]:
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.body.addWidget(group)
        return group, form

    def confirm_output(self, path: Path) -> Path | None:
        return confirm_output(self.window(), path, self.ctx.settings.overwrite_policy)

    def start_job(
        self,
        title: str,
        fn: Callable[[JobContext], JobResult],
        reveal: bool | None = None,
        on_success: Callable[[JobResult], None] | None = None,
    ) -> bool:
        return self.ctx.jobs.start(title, fn, on_success=on_success, reveal=reveal)


class SingleFilePanel(OperationPanel):
    """One input file -> one output file."""

    input_extensions: tuple[str, ...] = ()
    input_filter = "All files (*.*)"
    input_label = "Input file"
    output_suffix = "_edited"
    probe_media = True  # read length/streams with ffprobe when a file is chosen

    def build(self) -> None:
        group = QGroupBox(self.input_label)
        box = QVBoxLayout(group)
        self.picker = FilePicker(self.ctx, self.input_extensions, self.input_filter)
        self.picker.changed.connect(self._input_changed)
        self.input_info = label("", "muted")
        self.input_info.hide()
        box.addWidget(self.picker)
        box.addWidget(self.input_info)
        self.body.addWidget(group)
        self.media_info = None
        self.build_options()
        self.output = OutputPanel(self.ctx)
        self.body.addWidget(self.output)
        self.output.set_extension(self.output_extension(None))

    # -- hooks -------------------------------------------------------------
    def build_options(self) -> None:
        """Add option widgets (between input and output)."""

    def output_extension(self, source: Path | None) -> str:
        return source.suffix.lstrip(".").lower() if source else ""

    def suggested_stem(self, source: Path) -> str:
        return f"{source.stem}{self.output_suffix}"

    def on_media_info(self, info) -> None:
        """Called on the GUI thread after the chosen file was probed."""

    def make_job(self, source: Path) -> Callable[[Path, JobContext], JobResult]:
        """Validate the options (raise AppError if invalid) and return the work
        as a function of (output path, job context). Runs on the GUI thread;
        the returned function runs on a worker thread and must not touch widgets."""
        raise NotImplementedError

    def job_title(self, source: Path) -> str:
        return f"{self.title}: {source.name}"

    # -- behaviour ---------------------------------------------------------
    def refresh_output_extension(self) -> None:
        if hasattr(self, "output"):  # hooks may fire while the panel is still being built
            self.output.set_extension(self.output_extension(self.picker.path()))

    def _input_changed(self, path: Path | None) -> None:
        self.media_info = None
        self.input_info.hide()
        if path is None:
            return
        self.output.suggest_folder(self.ctx.output_dir_for(path))
        self.output.suggest_name(self.suggested_stem(path), force=True)
        self.refresh_output_extension()
        if self.probe_media:
            self._probe(path)

    def _probe(self, path: Path) -> None:
        token = object()
        self._probe_token = token
        self.input_info.setText("Reading file information...")
        self.input_info.show()

        def work():
            from app.services.ffmpeg.probe import probe

            return probe(self.ctx.tools.media_tools().ffprobe, path)

        def done(info) -> None:
            if getattr(self, "_probe_token", None) is not token:
                return  # another file was chosen meanwhile
            self.media_info = info
            self.input_info.setText(describe_short(info))
            self.on_media_info(info)
            self.refresh_output_extension()

        def failed(exc: BaseException) -> None:
            if getattr(self, "_probe_token", None) is not token:
                return
            message = exc.message if isinstance(exc, AppError) else str(exc)
            self.input_info.setText(f"⚠ {message}")

        run_in_background(work, done, failed, owner=self)

    def require_input(self) -> Path:
        source = self.picker.path()
        if source is None:
            raise InvalidInputError("Please choose an input file first.")
        return source

    def run(self) -> None:
        source = self.require_input()
        work = self.make_job(source)  # validates options first
        output = self.output.output_path()
        final = self.confirm_output(output)
        if final is None:
            return
        self.start_job(self.job_title(source), lambda ctx: work(final, ctx), reveal=self.output.reveal_when_done())


def describe_short(info) -> str:
    """One-line summary shown under the file picker."""
    from app.utils.timefmt import format_duration
    from app.utils.units import human_size

    parts = [f"Length {format_duration(info.best_duration)}"]
    video = info.primary_video
    if video is not None:
        size = video.display_size
        if size:
            parts.append(f"{size[0]}x{size[1]}")
        if video.fps:
            parts.append(f"{video.fps:.3g} fps")
        parts.append(video.codec.upper())
    audio = info.primary_audio
    if audio is not None:
        rate = f" {audio.bit_rate // 1000} kbps" if audio.bit_rate else ""
        parts.append(f"audio {audio.codec.upper()}{rate}")
        if len(info.audio_streams) > 1:
            parts.append(f"{len(info.audio_streams)} audio tracks")
    elif video is not None:
        parts.append("no audio")
    parts.append(human_size(info.size))
    return " · ".join(parts)


class InfoPanel(SingleFilePanel):
    """Shows information about a file (no output file)."""

    action_text = "Show information"
    probe_media = False

    def build(self) -> None:
        group = QGroupBox(self.input_label)
        box = QVBoxLayout(group)
        self.picker = FilePicker(self.ctx, self.input_extensions, self.input_filter)
        self.picker.changed.connect(lambda path: path and self._on_action())
        box.addWidget(self.picker)
        self.body.addWidget(group)
        self.view = monospace_view(260)
        self.body.addWidget(self.view)

    def describer(self) -> Callable[[Path, JobContext], JobResult]:
        """Return the function that reads the information (created on the GUI
        thread so a missing FFmpeg is reported immediately)."""
        raise NotImplementedError

    def run(self) -> None:
        source = self.require_input()
        describe = self.describer()
        self.view.setPlainText("Reading...")

        def show(result: JobResult) -> None:
            self.view.setPlainText(result.details)

        self.start_job(f"Reading {source.name}", lambda ctx: describe(source, ctx), reveal=False, on_success=show)


class OperationsPage(QWidget):
    """A module page: list of operations on the left, the chosen panel on the right."""

    def __init__(self, ctx, operations: list[tuple[str, str, type[OperationPanel]]], parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._operations = operations
        self._panels: dict[str, QWidget] = {}

        self.nav = QListWidget()
        self.nav.setObjectName("operationList")
        self.nav.setFixedWidth(230)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        for key, text, _ in operations:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
        self.stack = QStackedWidget()
        self.nav.currentRowChanged.connect(self._show_row)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.nav.setCurrentRow(0)

    def _show_row(self, row: int) -> None:
        if row < 0:
            return
        key, _, panel_class = self._operations[row]
        if key not in self._panels:
            panel = panel_class(self.ctx)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            panel.setObjectName("scrollContent")
            self._panels[key] = scroll
            self.stack.addWidget(scroll)
        self.stack.setCurrentWidget(self._panels[key])

    def show_operation(self, key: str) -> None:
        for row, (op_key, _, _) in enumerate(self._operations):
            if op_key == key:
                self.nav.setCurrentRow(row)
                return

    def panel(self, key: str) -> OperationPanel | None:
        scroll = self._panels.get(key)
        return scroll.widget() if scroll else None
