"""AUDIO / MUSIC module."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QGroupBox, QLineEdit, QSpinBox, QVBoxLayout

from app.core.errors import InvalidInputError
from app.services.audio import EDITABLE_TAGS
from app.services.ffmpeg.codecs import AUDIO_FORMATS, AUDIO_INPUT_EXTENSIONS, audio_format_for_ext, audio_output_ext
from app.ui.jobs import run_in_background
from app.ui.pages.base import InfoPanel, OperationPanel, OperationsPage, SingleFilePanel
from app.ui.pages.video import VIDEO_EXTS, ExtractAudioPanel
from app.ui.widgets.common import combo, hint, label
from app.ui.widgets.file_list import FileListWidget
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.output_panel import OutputPanel
from app.utils.timefmt import format_timestamp, parse_time

AUDIO_EXTS = tuple(f".{e}" for e in AUDIO_INPUT_EXTENSIONS)
AUDIO_FILTER = "Audio files (" + " ".join(f"*{e}" for e in AUDIO_EXTS) + ");;All files (*.*)"
MEDIA_EXTS = AUDIO_EXTS + VIDEO_EXTS
MEDIA_FILTER = "Audio and video files (" + " ".join(f"*{e}" for e in MEDIA_EXTS) + ");;All files (*.*)"
BITRATES = {b: f"{b} kbps" for b in (64, 96, 128, 160, 192, 256, 320)}


class _AudioPanel(SingleFilePanel):
    input_extensions = AUDIO_EXTS
    input_filter = AUDIO_FILTER
    input_label = "Audio file"

    def output_extension(self, source):
        return audio_output_ext(source.suffix) if source else "mp3"

    def service(self):
        return self.ctx.audio_service()


class AudioInfoPanel(InfoPanel):
    key = "info"
    title = "Audio information"
    description = "See the length, format, bitrate, sample rate, channels and tags of an audio file."
    input_extensions = MEDIA_EXTS
    input_filter = MEDIA_FILTER
    input_label = "Audio file"

    def describer(self):
        return self.ctx.audio_service().info


class AudioTrimPanel(_AudioPanel):
    key = "trim"
    title = "Trim audio"
    description = "Keep only part of an audio file. Enter the start and end as MM:SS or HH:MM:SS."
    action_text = "Trim"
    output_suffix = "_trimmed"

    def build_options(self) -> None:
        _, form = self.form_group("Part to keep")
        self.start = QLineEdit("00:00:00")
        self.end = QLineEdit()
        self.end.setPlaceholderText("end of the file")
        form.addRow("Start:", self.start)
        form.addRow("End:", self.end)

    def on_media_info(self, info) -> None:
        if info.best_duration:
            self.end.setText(format_timestamp(info.best_duration))

    def make_job(self, source):
        try:
            start = parse_time(self.start.text())
            end = parse_time(self.end.text()) if self.end.text().strip() else None
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from None
        if end is not None and end <= start:
            raise InvalidInputError("The end time must be after the start time.")
        service = self.service()
        return lambda output, ctx: service.trim(source, output, start, end, ctx)


class AudioMergePanel(OperationPanel):
    key = "merge"
    title = "Merge audio"
    description = "Join several audio files into one, in the order of the list."
    action_text = "Merge"

    def build(self) -> None:
        self.files = FileListWidget(self.ctx, AUDIO_EXTS, AUDIO_FILTER, noun="audio file", allow_folder=True)
        self.files.changed.connect(self._files_changed)
        self.body.addWidget(self.files, 1)
        _, form = self.form_group("Result format")
        preferred = self.ctx.settings.audio_format if self.ctx.settings.audio_format in AUDIO_FORMATS else "mp3"
        self.format = combo({k: f.label for k, f in AUDIO_FORMATS.items()}, preferred)
        self.bitrate = combo(BITRATES, self.ctx.settings.audio_bitrate)
        self.format.currentIndexChanged.connect(self._format_changed)
        form.addRow("Format:", self.format)
        form.addRow("Bitrate:", self.bitrate)
        self.output = OutputPanel(self.ctx)
        self.output.suggest_name("merged")
        self.output.suggest_folder(self.ctx.output_dir_for(None))
        self.body.addWidget(self.output)
        self._format_changed()

    def _format_changed(self) -> None:
        fmt = AUDIO_FORMATS[self.format.currentData()]
        self.bitrate.setEnabled(fmt.uses_bitrate)
        self.output.set_extension(fmt.ext)

    def _files_changed(self) -> None:
        paths = self.files.paths()
        if paths:
            self.output.suggest_folder(self.ctx.output_dir_for(paths[0]))

    def run(self) -> None:
        sources = self.files.paths()
        if len(sources) < 2:
            raise InvalidInputError("Please add at least two audio files.")
        service = self.ctx.audio_service()
        bitrate = self.bitrate.currentData()
        output = self.confirm_output(self.output.output_path())
        if output is None:
            return
        self.start_job(f"Merging {len(sources)} audio files", lambda ctx: service.merge(sources, output, bitrate, ctx),
                       reveal=self.output.reveal_when_done())


class AudioExtractPanel(ExtractAudioPanel):
    title = "Extract audio from video"


class AudioConvertPanel(_AudioPanel):
    key = "convert"
    title = "Convert / change bitrate"
    description = ("Convert to MP3, M4A, WAV, FLAC, OPUS or OGG, or keep the format and change the bitrate. "
                   "Tags and cover art are kept where the format supports them.")
    action_text = "Convert"
    input_extensions = MEDIA_EXTS
    input_filter = MEDIA_FILTER
    output_suffix = ""

    def build_options(self) -> None:
        _, form = self.form_group("Result")
        choices = {"same": "Same format as the original (change bitrate)"} | {k: f.label for k, f in AUDIO_FORMATS.items()}
        preferred = self.ctx.settings.audio_format if self.ctx.settings.audio_format in AUDIO_FORMATS else "mp3"
        self.format = combo(choices, preferred)
        self.bitrate = combo(BITRATES, self.ctx.settings.audio_bitrate)
        self.format.currentIndexChanged.connect(self._format_changed)
        form.addRow("Format:", self.format)
        form.addRow("Bitrate:", self.bitrate)
        form.addRow("", hint("Converting cannot improve quality beyond the original; a higher bitrate only makes the file larger."))
        self._format_changed()

    def _format(self, source: Path | None):
        key = self.format.currentData()
        if key == "same":
            return audio_format_for_ext(source.suffix) if source else None
        return AUDIO_FORMATS[key]

    def _format_changed(self) -> None:
        fmt = self._format(self.picker.path())
        self.bitrate.setEnabled(fmt is None or fmt.uses_bitrate)
        self.refresh_output_extension()
        source = self.picker.path()
        if source is not None and hasattr(self, "output"):
            self.output.suggest_name(self.suggested_stem(source), force=True)

    def suggested_stem(self, source: Path) -> str:
        key = self.format.currentData() if hasattr(self, "format") else "mp3"
        return f"{source.stem}_{self.bitrate.currentData()}k" if key == "same" else source.stem

    def output_extension(self, source):
        if not hasattr(self, "format"):
            return "mp3"
        fmt = self._format(source)
        return fmt.ext if fmt else "mp3"

    def make_job(self, source):
        fmt = self._format(source)
        if fmt is None:
            raise InvalidInputError(f"'{source.suffix}' files cannot be written. Choose one of the listed formats.")
        service, bitrate = self.service(), self.bitrate.currentData()
        return lambda output, ctx: service.convert(source, output, fmt.key, bitrate, ctx)


class AudioVolumePanel(_AudioPanel):
    key = "volume"
    title = "Change volume"
    description = "Make an audio file louder or quieter. The format stays the same."
    action_text = "Change volume"
    output_suffix = "_volume"

    def build_options(self) -> None:
        _, form = self.form_group("Volume")
        self.volume = QSpinBox()
        self.volume.setRange(0, 500)
        self.volume.setValue(150)
        self.volume.setSuffix(" %")
        form.addRow("New volume:", self.volume)
        form.addRow("", hint("100 % keeps the volume. Values above 100 % can distort audio that is already loud."))

    def make_job(self, source):
        factor = self.volume.value() / 100
        if abs(factor - 1.0) < 1e-9:
            raise InvalidInputError("The volume is 100 % (unchanged). Choose a different value.")
        service = self.service()
        return lambda output, ctx: service.change_volume(source, output, factor, ctx)


class FadePanel(_AudioPanel):
    key = "fade"
    title = "Fade in / fade out"
    description = "Let the sound rise smoothly at the start and/or fall smoothly at the end."
    action_text = "Add fades"
    output_suffix = "_faded"

    def build_options(self) -> None:
        _, form = self.form_group("Fades")
        self.fade_in = QDoubleSpinBox()
        self.fade_out = QDoubleSpinBox()
        for box, value in ((self.fade_in, 3.0), (self.fade_out, 3.0)):
            box.setRange(0, 600)
            box.setDecimals(1)
            box.setSingleStep(0.5)
            box.setSuffix(" s")
            box.setValue(value)
        form.addRow("Fade in:", self.fade_in)
        form.addRow("Fade out:", self.fade_out)
        form.addRow("", hint("Use 0 to skip a fade."))

    def make_job(self, source):
        fade_in, fade_out = self.fade_in.value(), self.fade_out.value()
        if fade_in == 0 and fade_out == 0:
            raise InvalidInputError("Please enter a fade-in and/or fade-out length.")
        service = self.service()
        return lambda output, ctx: service.fade(source, output, fade_in, fade_out, ctx)


class TagsPanel(OperationPanel):
    key = "tags"
    title = "Edit tags (metadata)"
    description = ("Change the title, artist, album and other details shown by music players. "
                   "The sound itself is not changed.")
    action_text = "Save tags"

    def build(self) -> None:
        group = QGroupBox("Audio file")
        box = QVBoxLayout(group)
        self.picker = FilePicker(self.ctx, AUDIO_EXTS, AUDIO_FILTER)
        self.picker.changed.connect(self._load)
        self.status = label("", "muted")
        box.addWidget(self.picker)
        box.addWidget(self.status)
        self.body.addWidget(group)
        _, form = self.form_group("Tags")
        self.fields: dict[str, QLineEdit] = {}
        for key, text in EDITABLE_TAGS:
            edit = QLineEdit()
            edit.setEnabled(False)
            self.fields[key] = edit
            form.addRow(f"{text}:", edit)
        self.in_place = QCheckBox("Save the changes into the original file")
        self.in_place.setChecked(True)
        self.in_place.toggled.connect(lambda on: self.output.setVisible(not on))
        self.body.addWidget(self.in_place)
        self.output = OutputPanel(self.ctx, title="Save a copy as")
        self.output.setVisible(False)
        self.body.addWidget(self.output)

    def _load(self, path: Path | None) -> None:
        for edit in self.fields.values():
            edit.clear()
            edit.setEnabled(False)
        if path is None:
            self.status.setText("")
            return
        self.output.suggest_folder(self.ctx.output_dir_for(path))
        self.output.suggest_name(f"{path.stem}_tagged", force=True)
        self.output.set_extension(path.suffix.lstrip("."))
        self.status.setText("Reading tags...")
        token = object()
        self._token = token
        service = self.ctx.audio_service()

        def done(tags: dict) -> None:
            if self._token is not token:
                return
            for key, edit in self.fields.items():
                edit.setText(tags.get(key, ""))
                edit.setEnabled(True)
            self.status.setText("Edit the fields and press Save tags. Empty fields remove the tag.")

        def failed(exc: BaseException) -> None:
            if self._token is token:
                self.status.setText(f"⚠ {getattr(exc, 'message', exc)}")

        run_in_background(lambda: service.read_tags(path), done, failed, owner=self)

    def run(self) -> None:
        source = self.picker.path()
        if source is None:
            raise InvalidInputError("Please choose an audio file first.")
        if not any(edit.isEnabled() for edit in self.fields.values()):
            raise InvalidInputError("The tags are still being read. Please wait a moment.")
        tags = {key: edit.text() for key, edit in self.fields.items()}
        service = self.ctx.audio_service()
        if self.in_place.isChecked():
            output = source
        else:
            output = self.confirm_output(self.output.output_path())
            if output is None:
                return
        self.start_job(f"Saving tags: {source.name}", lambda ctx: service.write_tags(source, output, tags, ctx),
                       reveal=False if self.in_place.isChecked() else self.output.reveal_when_done())


def build_audio_page(ctx) -> OperationsPage:
    from app.ui.pages.downloads import AudioDownloadPanel

    operations = [
        ("download", "Download from a link", AudioDownloadPanel),
        ("info", "Audio information", AudioInfoPanel),
        ("trim", "Trim", AudioTrimPanel),
        ("merge", "Merge audio", AudioMergePanel),
        ("extract", "Extract audio from video", AudioExtractPanel),
        ("convert", "Convert / change bitrate", AudioConvertPanel),
        ("volume", "Change volume", AudioVolumePanel),
        ("fade", "Fade in / out", FadePanel),
        ("tags", "Edit tags", TagsPanel),
    ]
    return OperationsPage(ctx, operations)

