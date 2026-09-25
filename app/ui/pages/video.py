"""VIDEO module."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QButtonGroup, QCheckBox, QDoubleSpinBox, QHBoxLayout, QLineEdit, QRadioButton, QSpinBox, QWidget

from app.core.errors import InvalidInputError
from app.services.ffmpeg.codecs import (
    AUDIO_FORMATS,
    VIDEO_INPUT_EXTENSIONS,
    VIDEO_OUTPUT_FORMATS,
    copy_audio_ext,
    video_output_ext,
)
from app.services.video import COMPRESSION_LEVELS, TRANSFORMS
from app.ui.pages.base import InfoPanel, OperationPanel, OperationsPage, SingleFilePanel
from app.ui.widgets.common import combo, hint
from app.ui.widgets.file_list import FileListWidget
from app.ui.widgets.output_panel import OutputPanel
from app.utils.timefmt import format_timestamp, parse_time

VIDEO_EXTS = tuple(f".{e}" for e in VIDEO_INPUT_EXTENSIONS)
VIDEO_FILTER = "Video files (" + " ".join(f"*{e}" for e in VIDEO_EXTS) + ");;All files (*.*)"
BITRATES = {b: f"{b} kbps" for b in (96, 128, 160, 192, 256, 320)}


class _VideoPanel(SingleFilePanel):
    input_extensions = VIDEO_EXTS
    input_filter = VIDEO_FILTER
    input_label = "Video"

    def output_extension(self, source: Path | None) -> str:
        return video_output_ext(source.suffix) if source else "mp4"

    def service(self):
        return self.ctx.video_service()


class VideoInfoPanel(InfoPanel):
    key = "info"
    title = "Video information"
    description = "See the length, resolution, codecs, audio tracks and size of a video."
    input_extensions = VIDEO_EXTS
    input_filter = VIDEO_FILTER
    input_label = "Video"

    def describer(self):
        return self.ctx.video_service().info


class TrimPanel(_VideoPanel):
    key = "trim"
    title = "Trim video"
    description = "Keep only part of a video. Enter the start and end as HH:MM:SS (for example 00:01:30)."
    action_text = "Trim"
    output_suffix = "_trimmed"

    def build_options(self) -> None:
        _, form = self.form_group("Part to keep")
        self.start = QLineEdit("00:00:00")
        self.end = QLineEdit()
        self.end.setPlaceholderText("end of the video")
        self.fast = QCheckBox("Fast mode - no re-encoding, but the cut may start a little early (at a keyframe)")
        form.addRow("Start:", self.start)
        form.addRow("End:", self.end)
        form.addRow("", self.fast)

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
        service, precise = self.service(), not self.fast.isChecked()
        return lambda output, ctx: service.trim(source, output, start, end, precise, ctx)


class MergeVideoPanel(OperationPanel):
    key = "merge"
    title = "Merge videos"
    description = ("Join several videos into one, in the order of the list. Matching videos are joined instantly "
                   "without quality loss; different sizes or formats are converted to match the first video.")
    action_text = "Merge"

    def build(self) -> None:
        self.files = FileListWidget(self.ctx, VIDEO_EXTS, VIDEO_FILTER, noun="video", allow_folder=True)
        self.files.changed.connect(self._files_changed)
        self.body.addWidget(self.files, 1)
        _, form = self.form_group("Result format")
        self.format = combo(VIDEO_OUTPUT_FORMATS, "mp4")
        self.format.currentIndexChanged.connect(lambda: self.output.set_extension(self.format.currentData()))
        form.addRow("Format:", self.format)
        self.output = OutputPanel(self.ctx)
        self.output.suggest_name("merged")
        self.output.set_extension("mp4")
        self.output.suggest_folder(self.ctx.output_dir_for(None))
        self.body.addWidget(self.output)

    def _files_changed(self) -> None:
        paths = self.files.paths()
        if paths:
            self.output.suggest_folder(self.ctx.output_dir_for(paths[0]))

    def run(self) -> None:
        sources = self.files.paths()
        if len(sources) < 2:
            raise InvalidInputError("Please add at least two videos.")
        service = self.ctx.video_service()
        output = self.confirm_output(self.output.output_path())
        if output is None:
            return
        self.start_job(f"Merging {len(sources)} videos", lambda ctx: service.merge(sources, output, ctx),
                       reveal=self.output.reveal_when_done())


class ExtractAudioPanel(_VideoPanel):
    key = "extract_audio"
    title = "Extract audio"
    description = "Save the sound of a video as an audio file."
    action_text = "Extract audio"
    output_suffix = ""

    def build_options(self) -> None:
        _, form = self.form_group("Audio format")
        choices = {"copy": "Original format - no quality loss"} | {k: f.label for k, f in AUDIO_FORMATS.items()}
        self.format = combo(choices, "copy")
        self.bitrate = combo(BITRATES, self.ctx.settings.audio_bitrate)
        self.track = combo({})
        self.track.setEnabled(False)
        self.format.currentIndexChanged.connect(self._format_changed)
        self.track.currentIndexChanged.connect(self.refresh_output_extension)
        form.addRow("Format:", self.format)
        form.addRow("Bitrate:", self.bitrate)
        form.addRow("Audio track:", self.track)
        self._format_changed()

    def _format_changed(self) -> None:
        key = self.format.currentData()
        self.bitrate.setEnabled(key != "copy" and AUDIO_FORMATS[key].uses_bitrate)
        self.refresh_output_extension()

    def on_media_info(self, info) -> None:
        self.track.clear()
        for stream in info.audio_streams:
            text = f"Track {stream.index}: {stream.codec.upper()}"
            if stream.language and stream.language != "und":
                text += f" ({stream.language})"
            if stream.title:
                text += f" - {stream.title}"
            self.track.addItem(text, stream.index)
        self.track.setEnabled(len(info.audio_streams) > 1)

    def output_extension(self, source: Path | None) -> str:
        key = self.format.currentData() if hasattr(self, "format") else "copy"
        if key != "copy":
            return AUDIO_FORMATS[key].ext
        if self.media_info is not None and self.media_info.audio_streams:
            index = self.track.currentData()
            stream = next((s for s in self.media_info.audio_streams if s.index == index), self.media_info.audio_streams[0])
            return copy_audio_ext(stream.codec)
        return "m4a"

    def make_job(self, source):
        if self.format.currentData() == "copy" and self.media_info is None:
            raise InvalidInputError("Please wait until the file information has been read.")
        service = self.service()
        target = self.format.currentData()
        bitrate = self.bitrate.currentData()
        track = self.track.currentData()
        return lambda output, ctx: service.extract_audio(source, output, target, bitrate, ctx, track)


class RemoveAudioPanel(_VideoPanel):
    key = "remove_audio"
    title = "Remove audio"
    description = "Make a silent copy of a video. The picture is copied without any quality loss."
    action_text = "Remove audio"
    output_suffix = "_no_audio"

    def make_job(self, source):
        service = self.service()
        return lambda output, ctx: service.remove_audio(source, output, ctx)


class ExtractVideoPanel(_VideoPanel):
    key = "extract_video"
    title = "Extract video stream"
    description = "Save only the picture of a video (no sound, no subtitles), without quality loss where possible."
    action_text = "Extract video"
    output_suffix = "_video_only"

    def make_job(self, source):
        service = self.service()
        return lambda output, ctx: service.extract_video(source, output, ctx)


class ConvertVideoPanel(_VideoPanel):
    key = "convert"
    title = "Convert format"
    description = ("Change the file format. When the video and audio already fit the new format they are copied "
                   "without quality loss; otherwise they are converted.")
    action_text = "Convert"
    output_suffix = ""

    def build_options(self) -> None:
        _, form = self.form_group("New format")
        preferred = self.ctx.settings.video_container if self.ctx.settings.video_container in VIDEO_OUTPUT_FORMATS else "mp4"
        self.format = combo(VIDEO_OUTPUT_FORMATS, preferred)
        self.format.currentIndexChanged.connect(self.refresh_output_extension)
        self.reencode = QCheckBox("Always re-encode (slower; use if the result does not play on a device)")
        form.addRow("Format:", self.format)
        form.addRow("", self.reencode)

    def output_extension(self, source):
        return self.format.currentData() if hasattr(self, "format") else "mp4"

    def make_job(self, source):
        service, force = self.service(), self.reencode.isChecked()
        return lambda output, ctx: service.convert(source, output, force, ctx)


class ResizePanel(_VideoPanel):
    key = "resize"
    title = "Resize video"
    description = "Change the picture size (resolution). The sound is kept as it is."
    action_text = "Resize"
    PRESETS = {2160: "2160p (4K)", 1440: "1440p", 1080: "1080p (Full HD)", 720: "720p (HD)", 480: "480p", 360: "360p",
               0: "Custom size"}

    def build_options(self) -> None:
        _, form = self.form_group("New size")
        self.preset = combo(self.PRESETS, 720)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.width = QSpinBox()
        self.height = QSpinBox()
        for box in (self.width, self.height):
            box.setRange(16, 8192)
            box.setSingleStep(2)
        self.width.setValue(1280)
        self.height.setValue(720)
        self.keep_aspect = QCheckBox("Keep proportions (fit inside the size)")
        self.keep_aspect.setChecked(True)
        custom = QWidget()
        row = QHBoxLayout(custom)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.width)
        row.addWidget(hint("x"))
        row.addWidget(self.height)
        row.addWidget(self.keep_aspect)
        row.addStretch(1)
        form.addRow("Preset:", self.preset)
        form.addRow("Custom:", custom)
        self._custom = custom
        self._preset_changed()

    def _preset_changed(self) -> None:
        self._custom.setEnabled(self.preset.currentData() == 0)
        source = self.picker.path() if hasattr(self, "picker") else None
        if source is not None and hasattr(self, "output"):
            self.output.suggest_name(self.suggested_stem(source), force=True)

    def suggested_stem(self, source: Path) -> str:
        height = self.preset.currentData() if hasattr(self, "preset") else 720
        return f"{source.stem}_{height}p" if height else f"{source.stem}_resized"

    def make_job(self, source):
        service = self.service()
        height = self.preset.currentData()
        if height:
            width, keep = None, True
        else:
            width, height, keep = self.width.value(), self.height.value(), self.keep_aspect.isChecked()
        return lambda output, ctx: service.resize(source, output, width, height, keep, ctx)


class RotatePanel(_VideoPanel):
    key = "rotate"
    title = "Rotate or flip"
    description = "Turn a video that was filmed sideways, or mirror it."
    action_text = "Apply"
    LABELS = {"cw": "Rotate 90° clockwise", "ccw": "Rotate 90° counter-clockwise", "180": "Rotate 180°",
              "hflip": "Flip horizontally (mirror)", "vflip": "Flip vertically (upside down)"}

    def build_options(self) -> None:
        group, form = self.form_group("Change")
        self.buttons = QButtonGroup(self)
        for index, (key, text) in enumerate(self.LABELS.items()):
            radio = QRadioButton(text)
            radio.setProperty("op", key)
            self.buttons.addButton(radio, index)
            form.addRow(radio)
            if index == 0:
                radio.setChecked(True)
        self.buttons.buttonClicked.connect(lambda _: self._update_name())

    def _operation(self) -> str:
        return self.buttons.checkedButton().property("op")

    def _update_name(self) -> None:
        source = self.picker.path()
        if source is not None:
            self.output.suggest_name(self.suggested_stem(source), force=True)

    def suggested_stem(self, source: Path) -> str:
        op = self._operation() if hasattr(self, "buttons") else "cw"
        return f"{source.stem}_{'flipped' if 'flip' in op else 'rotated'}"

    def make_job(self, source):
        service, op = self.service(), self._operation()
        assert op in TRANSFORMS
        return lambda output, ctx: service.transform(source, output, op, ctx)


class SpeedPanel(_VideoPanel):
    key = "speed"
    title = "Change speed"
    description = "Speed up or slow down a video (0.25x to 4x). The sound keeps its normal pitch."
    action_text = "Change speed"

    def build_options(self) -> None:
        _, form = self.form_group("Speed")
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.25, 4.0)
        self.speed.setSingleStep(0.25)
        self.speed.setValue(1.5)
        self.speed.setSuffix(" x")
        self.speed.valueChanged.connect(self._update_name)
        form.addRow("Speed:", self.speed)
        form.addRow("", hint("1 x is normal speed. 2 x is twice as fast; 0.5 x is half speed."))

    def _update_name(self) -> None:
        source = self.picker.path()
        if source is not None:
            self.output.suggest_name(self.suggested_stem(source), force=True)

    def suggested_stem(self, source: Path) -> str:
        value = self.speed.value() if hasattr(self, "speed") else 1.5
        return f"{source.stem}_{value:g}x"

    def make_job(self, source):
        factor = self.speed.value()
        if abs(factor - 1.0) < 1e-9:
            raise InvalidInputError("The speed is 1 x (unchanged). Choose a different speed.")
        service = self.service()
        return lambda output, ctx: service.change_speed(source, output, factor, ctx)


class VolumePanel(_VideoPanel):
    key = "volume"
    title = "Change volume"
    description = "Make the sound of a video louder or quieter. The picture is copied without quality loss."
    action_text = "Change volume"

    def build_options(self) -> None:
        _, form = self.form_group("Volume")
        self.volume = QSpinBox()
        self.volume.setRange(0, 500)
        self.volume.setValue(150)
        self.volume.setSuffix(" %")
        form.addRow("New volume:", self.volume)
        form.addRow("", hint("100 % keeps the volume. Values above 100 % can distort audio that is already loud."))

    def suggested_stem(self, source: Path) -> str:
        return f"{source.stem}_volume"

    def make_job(self, source):
        factor = self.volume.value() / 100
        if abs(factor - 1.0) < 1e-9:
            raise InvalidInputError("The volume is 100 % (unchanged). Choose a different value.")
        service = self.service()
        return lambda output, ctx: service.change_volume(source, output, factor, ctx)


class CompressPanel(_VideoPanel):
    key = "compress"
    title = "Compress video"
    description = "Make a video file smaller, for example to share it. The result is an MP4 file."
    action_text = "Compress"
    output_suffix = "_compressed"

    def build_options(self) -> None:
        _, form = self.form_group("Compression")
        self.level = combo({k: v[0] for k, v in COMPRESSION_LEVELS.items()}, "balanced")
        self.max_height = combo({0: "Keep the resolution", 1080: "At most 1080p", 720: "At most 720p",
                                 480: "At most 480p"}, 0)
        form.addRow("Quality:", self.level)
        form.addRow("Resolution:", self.max_height)

    def output_extension(self, source):
        return "mp4"

    def make_job(self, source):
        service = self.service()
        level, height = self.level.currentData(), self.max_height.currentData() or None
        return lambda output, ctx: service.compress(source, output, level, height, ctx)


def build_video_page(ctx) -> OperationsPage:
    from app.ui.pages.downloads import VideoDownloadPanel

    operations = [
        ("download", "Download from a link", VideoDownloadPanel),
        ("info", "Video information", VideoInfoPanel),
        ("trim", "Trim", TrimPanel),
        ("merge", "Merge videos", MergeVideoPanel),
        ("extract_audio", "Extract audio", ExtractAudioPanel),
        ("remove_audio", "Remove audio", RemoveAudioPanel),
        ("extract_video", "Extract video stream", ExtractVideoPanel),
        ("convert", "Convert format", ConvertVideoPanel),
        ("resize", "Resize", ResizePanel),
        ("rotate", "Rotate / flip", RotatePanel),
        ("speed", "Change speed", SpeedPanel),
        ("volume", "Change volume", VolumePanel),
        ("compress", "Compress", CompressPanel),
    ]
    return OperationsPage(ctx, operations)

