"""Downloading: video/audio from websites (yt-dlp) and PDFs from direct links."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import AppError, InvalidInputError
from app.core.jobs import JobContext
from app.models.remote import FormatKind, RemoteMedia, recommended_formats
from app.models.results import JobResult
from app.services.downloads import http as pdf_http
from app.services.downloads.plans import (
    AUDIO_TARGETS,
    BITRATE_TARGETS,
    DownloadPlan,
    plan_audio_download,
    plan_video_download,
)
from app.services.downloads.ytdlp import DownloadTarget, get_backend
from app.ui import icons, theme
from app.ui.pages.base import OperationPanel
from app.ui.widgets.common import combo, hint, label
from app.ui.widgets.format_table import BEST, FormatTable
from app.ui.widgets.output_panel import OutputPanel
from app.utils.filenames import build_filename, sanitize_filename
from app.utils.timefmt import format_duration
from app.utils.units import human_size
from app.utils.urls import UrlError, validate_web_url

LEGAL_NOTE = ("Only download content you have the right to download. Signed-in, paid, DRM-protected and "
              "private content is not supported.")


class MediaDownloadPanel(OperationPanel):
    """Shared by the VIDEO and AUDIO download panels."""

    mode = "video"
    key = "download"
    action_text = "Download"

    def build(self) -> None:
        self.media: RemoteMedia | None = None
        self._analyzed_url = ""

        url_group = QGroupBox("Link")
        url_layout = QVBoxLayout(url_group)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Paste a link to a video page, e.g. https://www.youtube.com/watch?v=...")
        self.url.returnPressed.connect(self.analyze)
        self.url.textChanged.connect(self._url_edited)
        paste = QPushButton("Paste")
        paste.clicked.connect(lambda: self.url.setText(QGuiApplication.clipboard().text().strip()))
        self.analyze_button = QPushButton("Analyze formats")
        self.analyze_button.clicked.connect(self.analyze)
        row.addWidget(self.url, 1)
        row.addWidget(paste)
        row.addWidget(self.analyze_button)
        url_layout.addLayout(row)
        url_layout.addWidget(hint(LEGAL_NOTE))
        self.body.addWidget(url_group)

        self.summary = label("", "")
        self.summary.hide()
        self.body.addWidget(self.summary)

        self.formats_group = QGroupBox("Available formats")
        formats_layout = QVBoxLayout(self.formats_group)
        filters = QHBoxLayout()
        self.show_all = QCheckBox("Show all formats (including duplicate streaming variants)")
        self.show_all.toggled.connect(self._fill_table)
        filters.addWidget(self.show_all)
        if self.mode == "video":
            self.kind_filter = combo({"all": "All types", "combined": "Video + audio", "video": "Video only",
                                      "audio": "Audio only"}, "all")
            self.kind_filter.currentIndexChanged.connect(self._fill_table)
            filters.addStretch(1)
            filters.addWidget(QLabel("Show:"))
            filters.addWidget(self.kind_filter)
        else:
            filters.addStretch(1)
        formats_layout.addLayout(filters)
        self.table = FormatTable(self.mode)
        self.table.selection_changed.connect(lambda _: self._update_plan())
        formats_layout.addWidget(self.table)
        self.plan_label = label("", "")
        self.notes_label = label("", "warningText")
        formats_layout.addWidget(self.plan_label)
        formats_layout.addWidget(self.notes_label)
        self.body.addWidget(self.formats_group)

        _, form = self.form_group("Options")
        self.build_download_options(form)

        self.output = OutputPanel(self.ctx)
        self.output.suggest_folder(self.ctx.settings.effective_download_dir())
        self.output.set_extension(None)
        self.body.addWidget(self.output)
        self._set_analyzed(False)

    # -- mode-specific ---------------------------------------------------------
    def build_download_options(self, form) -> None:
        raise NotImplementedError

    def make_plan(self, selected) -> DownloadPlan:
        raise NotImplementedError

    def visible_formats(self, media: RemoteMedia) -> list:
        raise NotImplementedError

    def preselect(self) -> None:
        self.table.select_best()

    # -- analysis ----------------------------------------------------------
    def _url_edited(self) -> None:
        if self.media is not None and self.url.text().strip() != self._analyzed_url:
            self.plan_label.setText("The link changed - press Analyze formats again.")

    def _set_analyzed(self, analyzed: bool) -> None:
        self.formats_group.setEnabled(analyzed)
        self.output.setEnabled(analyzed)
        self.action_button.setEnabled(analyzed and not (self.ctx.jobs and self.ctx.jobs.busy))

    def _on_busy(self, busy: bool) -> None:
        self.action_button.setEnabled(not busy and self.media is not None)
        self.analyze_button.setEnabled(not busy)

    def analyze(self) -> None:
        try:
            url = validate_web_url(self.url.text())
            backend = get_backend(self.ctx.tools, self.ctx.settings.embed_metadata)
        except UrlError as exc:
            raise_to_dialog(self, InvalidInputError(str(exc)))
            return
        except AppError as exc:  # e.g. the external yt-dlp set in Settings is missing
            raise_to_dialog(self, exc)
            return
        self.url.setText(url)

        def work(ctx: JobContext) -> JobResult:
            media = backend.analyze(url, ctx)
            return JobResult(f"Found {len(media.formats)} formats: {media.title}", data=media)

        self.start_job("Analyzing link", work, reveal=False, on_success=lambda result: self._analyzed(url, result.data))

    def _analyzed(self, url: str, media: RemoteMedia) -> None:
        self.media = media
        self._analyzed_url = url
        details = [f"<b>{_escape(media.title)}</b>"]
        extra = [x for x in (media.uploader, format_duration(media.duration) if media.duration else "", media.extractor) if x]
        if extra:
            details.append(_escape("  ·  ".join(extra)))
        self.summary.setText("<br>".join(details))
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.show()
        self.output.suggest_name(media.title, force=True)
        self._fill_table()
        self.preselect()
        self._set_analyzed(True)
        self._update_plan()

    def _fill_table(self) -> None:
        if self.media is None:
            return
        formats = self.visible_formats(self.media)
        best_text = self._best_text()
        self.table.set_formats(formats, best_text)
        self.table.select_best()

    def _best_text(self) -> str:
        try:
            plan = self.make_plan(None)
        except Exception:  # noqa: BLE001
            return "chosen by yt-dlp"
        return plan.summary.removeprefix("Best available: ")

    def _selected(self):
        value = self.table.current_format()
        return None if value in (BEST, None) else value

    def _update_plan(self) -> None:
        if self.media is None:
            return
        try:
            plan = self.make_plan(self._selected())
        except ValueError as exc:
            self.plan_label.setText(str(exc))
            return
        self.plan_label.setText(f"Will download: {plan.summary}")
        self.notes_label.setText("\n".join(plan.notes))
        self.notes_label.setVisible(bool(plan.notes))
        self.output.set_extension(plan.final_ext)

    # -- download ------------------------------------------------------------
    def run(self) -> None:
        if self.media is None:
            raise InvalidInputError("Please paste a link and press Analyze formats first.")
        if self.url.text().strip() != self._analyzed_url:
            raise InvalidInputError("The link was changed after analyzing. Please press Analyze formats again.")
        plan = self.make_plan(self._selected())
        folder = self.output.folder_path()
        stem = self.output.base_name()
        approved = None
        if plan.final_ext:
            target = folder / build_filename(stem, plan.final_ext)
            final = self.confirm_output(target)
            if final is None:
                return
            if final == target and target.exists():
                approved = target
            stem = final.stem
        backend = get_backend(self.ctx.tools, self.ctx.settings.embed_metadata)
        url, title = self._analyzed_url, self.media.title
        target = DownloadTarget(folder, sanitize_filename(stem), approved)

        def work(ctx: JobContext) -> JobResult:
            path = backend.download(url, plan, target, ctx)
            return JobResult(f"Downloaded {path.name} ({human_size(path.stat().st_size)})", outputs=[path])

        self.start_job(f"Downloading: {title}", work, reveal=self.output.reveal_when_done())


class VideoDownloadPanel(MediaDownloadPanel):
    mode = "video"
    title = "Download video from a link"
    description = ("Works with YouTube and the many other sites supported by yt-dlp. Press Analyze formats to see "
                   "every quality the site offers, pick one (or keep Best available) and press Download.")

    def build_download_options(self, form) -> None:
        self.container = combo({"auto": "Automatic (MP4 when possible)", "mp4": "MP4", "mkv": "MKV", "webm": "WEBM"},
                               self.ctx.settings.video_container)
        self.container.currentIndexChanged.connect(self._options_changed)
        self.add_audio = QCheckBox("When a format has no sound, add the best matching audio automatically")
        self.add_audio.setChecked(self.ctx.settings.video_add_audio)
        self.add_audio.toggled.connect(self._options_changed)
        form.addRow("File format:", self.container)
        form.addRow("", self.add_audio)

    def _options_changed(self) -> None:
        if self.media is not None:
            self.table.item(0, 0).setText(f"★  Best available (automatic) - {self._best_text()}")
            self._update_plan()

    def visible_formats(self, media: RemoteMedia) -> list:
        formats = media.formats if self.show_all.isChecked() else recommended_formats(media.formats)
        kind = self.kind_filter.currentData()
        wanted = {"combined": FormatKind.COMBINED, "video": FormatKind.VIDEO_ONLY, "audio": FormatKind.AUDIO_ONLY}.get(kind)
        video = sorted((f for f in formats if f.has_video and (wanted is None or f.kind is wanted)),
                       key=lambda f: (f.height or 0, f.fps or 0, f.total_bitrate or 0), reverse=True)
        audio = [f for f in media.audio_formats if f in formats and (wanted in (None, FormatKind.AUDIO_ONLY))]
        return video + audio

    def preselect(self) -> None:
        preferred = self.ctx.settings.video_quality
        if preferred == "best" or self.media is None:
            self.table.select_best()
            return
        limit = int(preferred)
        candidates = [f for f in self.visible_formats(self.media) if f.has_video and f.height and f.height <= limit]
        if candidates:
            self.table.select_format(candidates[0])
        else:
            self.table.select_best()

    def make_plan(self, selected) -> DownloadPlan:
        return plan_video_download(self.media, selected, self.container.currentData(), self.add_audio.isChecked())


class AudioDownloadPanel(MediaDownloadPanel):
    mode = "audio"
    title = "Download audio / music from a link"
    description = ("Download only the sound from YouTube and the many other sites supported by yt-dlp, and "
                   "optionally convert it to MP3, M4A, WAV, FLAC or OPUS.")

    def build_download_options(self, form) -> None:
        self.target = combo(AUDIO_TARGETS, self.ctx.settings.audio_format)
        self.bitrate = combo({b: f"{b} kbps" for b in (96, 128, 160, 192, 256, 320)}, self.ctx.settings.audio_bitrate)
        self.target.currentIndexChanged.connect(self._options_changed)
        self.bitrate.currentIndexChanged.connect(self._options_changed)
        form.addRow("Save as:", self.target)
        form.addRow("Bitrate:", self.bitrate)
        form.addRow("", hint("Bitrate applies to MP3, M4A and OPUS. Converting cannot improve on the original quality."))
        self._options_changed()

    def _options_changed(self) -> None:
        self.bitrate.setEnabled(self.target.currentData() in BITRATE_TARGETS)
        if getattr(self, "media", None) is not None:
            self.table.item(0, 0).setText(f"★  Best available (automatic) - {self._best_text()}")
            self._update_plan()

    def visible_formats(self, media: RemoteMedia) -> list:
        formats = media.formats if self.show_all.isChecked() else recommended_formats(media.formats)
        audio = [f for f in media.audio_formats if f in formats]
        if audio:
            return audio
        # Some sites only offer video with sound; the audio is extracted from it.
        return sorted((f for f in formats if f.kind is FormatKind.COMBINED), key=lambda f: f.total_bitrate or 0, reverse=True)

    def make_plan(self, selected) -> DownloadPlan:
        return plan_audio_download(self.media, selected, self.target.currentData(), self.bitrate.currentData())


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class PdfDownloadPanel(OperationPanel):
    key = "download"
    title = "Download a PDF"
    description = ("Save a PDF from a direct web link (a link that opens the PDF file itself). "
                   "The file is checked to make sure it really is a PDF.")
    action_text = "Download PDF"

    def build(self) -> None:
        group = QGroupBox("PDF link")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com/document.pdf")
        self.url.editingFinished.connect(self._suggest_from_url)
        paste = QPushButton("Paste")
        paste.clicked.connect(self._paste)
        check = QPushButton("Check link")
        check.setToolTip("Look up the file name and size before downloading")
        check.clicked.connect(self._check)
        row.addWidget(self.url, 1)
        row.addWidget(paste)
        row.addWidget(check)
        layout.addLayout(row)
        self.info = label("", "muted")
        layout.addWidget(self.info)
        self.body.addWidget(group)
        self.output = OutputPanel(self.ctx)
        self.output.set_extension("pdf")
        self.output.suggest_folder(self.ctx.settings.effective_download_dir())
        self.body.addWidget(self.output)

    def _paste(self) -> None:
        self.url.setText(QGuiApplication.clipboard().text().strip())
        self._suggest_from_url()

    def _suggest_from_url(self) -> None:
        try:
            url = validate_web_url(self.url.text())
        except UrlError:
            return
        self.output.suggest_name(Path(pdf_http.suggest_pdf_filename(url)).stem)

    def _check(self) -> None:
        try:
            url = validate_web_url(self.url.text())
        except UrlError as exc:
            raise_to_dialog(self, InvalidInputError(str(exc)))
            return

        def work(ctx: JobContext) -> JobResult:
            ctx.set_status("Checking link...")
            ctx.set_progress(None)
            info = pdf_http.inspect_link(url)
            return JobResult(f"Checked link: {info.filename}", data=info)

        def done(result: JobResult) -> None:
            info = result.data
            self.output.suggest_name(Path(info.filename).stem, force=True)
            size = human_size(info.size) if info.size else "unknown size"
            if info.is_pdf:
                self.info.setText(f"✓ {info.filename} · {size} · PDF confirmed")
                self.info.setStyleSheet(f"color: {theme.colors()['success']};")
            else:
                kind = info.content_type or "unknown type"
                self.info.setText(f"⚠ This link does not point directly to a PDF (the server sent '{kind}'). "
                                  "Use the direct link to the PDF file.")
                self.info.setStyleSheet(f"color: {theme.colors()['warning']};")

        self.start_job("Checking PDF link", work, reveal=False, on_success=done)

    def run(self) -> None:
        try:
            url = validate_web_url(self.url.text())
        except UrlError as exc:
            raise InvalidInputError(str(exc)) from None
        if not self.output.name.text().strip():
            self.output.suggest_name(Path(pdf_http.suggest_pdf_filename(url)).stem, force=True)
        output = self.confirm_output(self.output.output_path())
        if output is None:
            return
        self.start_job(f"Downloading {output.name}", lambda ctx: pdf_http.download_pdf(url, output, ctx),
                       reveal=self.output.reveal_when_done())


def raise_to_dialog(widget: QWidget, exc: Exception) -> None:
    from app.ui.dialogs import show_error

    show_error(widget.window(), exc)


class DownloadHubPage(QWidget):
    """DOWNLOAD home button: choose what to download."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(16)
        heading = QLabel("What would you like to download?")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        layout.addWidget(label(LEGAL_NOTE, "muted"))
        grid = QGridLayout()
        grid.setSpacing(16)
        choices = [
            ("video", "Video", "From YouTube and many other video sites. Choose the exact quality.", ("video", "download")),
            ("audio", "Audio / music", "Only the sound, optionally converted to MP3, M4A, FLAC and more.", ("audio", "download")),
            ("pdf", "PDF document", "From a direct link to a PDF file.", ("pdf", "download")),
        ]
        from app.ui.home import ModuleCard

        for column, (icon_name, title, text, target) in enumerate(choices):
            card = ModuleCard(icons.pixmap(icon_name, theme.colors()["accent"], 36), title, text)
            card.clicked.connect(lambda _=False, t=target: self.ctx.navigate(*t))
            grid.addWidget(card, 0, column)
        layout.addLayout(grid)
        layout.addStretch(1)
