"""SETTINGS page."""

from __future__ import annotations

from dataclasses import asdict
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
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import paths
from app.config.settings import Settings
from app.services.downloads.plans import AUDIO_TARGETS
from app.services.translation.languages import LANGUAGES
from app.ui.dialogs import confirm, show_info
from app.ui.widgets.common import combo, hint, primary_button
from app.utils.system import open_path


class SettingsPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        content = QWidget()
        content.setObjectName("scrollContent")
        content.setMaximumWidth(1000)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(14)
        title = QLabel("Settings")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        general = self._group(layout, "General")
        self.download_dir = self._folder_row(general, "Download folder:", "Empty = your Downloads folder")
        self.output_dir = self._folder_row(general, "Default output folder:", "Empty = the same folder as the input file")
        self.remember_dir = QCheckBox("Remember the last folder used in file dialogs")
        general.addRow("", self.remember_dir)
        self.overwrite = combo({"ask": "Ask me", "rename": "Keep both (add a number to the new file)",
                                "replace": "Replace without asking"})
        general.addRow("When a file already exists:", self.overwrite)
        self.open_after = QCheckBox("Open the output folder after a task finishes")
        general.addRow("", self.open_after)
        self.theme = combo({"system": "Same as Windows", "light": "Light", "dark": "Dark"})
        general.addRow("Theme:", self.theme)

        video = self._group(layout, "Video downloads")
        self.container = combo({"auto": "Automatic (MP4 when possible)", "mp4": "MP4", "mkv": "MKV", "webm": "WEBM"})
        video.addRow("Preferred file format:", self.container)
        self.quality = combo({"best": "Best available", "2160": "Up to 2160p (4K)", "1440": "Up to 1440p",
                              "1080": "Up to 1080p", "720": "Up to 720p", "480": "Up to 480p", "360": "Up to 360p"})
        video.addRow("Preferred quality:", self.quality)
        self.add_audio = QCheckBox("Add the best matching audio to formats without sound")
        video.addRow("Download behaviour:", self.add_audio)
        self.embed_metadata = QCheckBox("Write the title and uploader into downloaded files")
        video.addRow("", self.embed_metadata)

        audio = self._group(layout, "Audio")
        self.audio_format = combo(AUDIO_TARGETS)
        audio.addRow("Preferred format:", self.audio_format)
        self.audio_bitrate = combo({b: f"{b} kbps" for b in (96, 128, 160, 192, 256, 320)})
        audio.addRow("Preferred bitrate:", self.audio_bitrate)

        pdf = self._group(layout, "PDF")
        self.pdf_name = QLineEdit()
        pdf.addRow("Default name for new PDFs:", self.pdf_name)
        self.pdf_dpi = QSpinBox()
        self.pdf_dpi.setRange(36, 600)
        self.pdf_dpi.setSuffix(" DPI")
        pdf.addRow("Default image resolution:", self.pdf_dpi)

        translation = self._group(layout, "AI translation")
        self.translation_target = combo({lang.code: lang.label for lang in LANGUAGES})
        translation.addRow("Translate into:", self.translation_target)
        translation_row = QHBoxLayout()
        key_button = QPushButton("API key...")
        key_button.setToolTip("Enter, test, replace or remove your own AI API key")
        key_button.clicked.connect(lambda: self.ctx.navigate("translate", "key"))
        clear_progress = QPushButton("Clear saved translation progress")
        clear_progress.setToolTip("Forget pages translated by runs that did not finish")
        clear_progress.clicked.connect(self._clear_translation_progress)
        translation_row.addWidget(key_button)
        translation_row.addWidget(clear_progress)
        translation_row.addStretch(1)
        translation.addRow("", self._wrap(translation_row))
        translation.addRow("", hint("Translation uses your own Google Gemini API key. The key is kept in Windows "
                                    "Credential Manager, not in the settings file."))

        updates = self._group(layout, "Updates")
        self.check_updates = QCheckBox("Tell me when a new version is available (checked when the app starts)")
        updates.addRow("", self.check_updates)
        updates_row = QHBoxLayout()
        self.check_now = QPushButton("Check for updates now")
        self.check_now.clicked.connect(self._check_updates_now)
        updates_row.addWidget(self.check_now)
        updates_row.addStretch(1)
        updates.addRow("", self._wrap(updates_row))
        updates.addRow("", hint("The app asks GitHub, where new versions are published, whether there is a newer "
                                "one. Nothing personal is sent, and nothing is downloaded or installed "
                                "automatically - you decide."))

        advanced = self._group(layout, "Advanced")
        advanced.addRow("", hint("Leave these empty to use the programs included with the application."))
        self.ffmpeg = self._file_row(advanced, "FFmpeg (ffmpeg.exe):", "Automatic")
        self.ffprobe = self._file_row(advanced, "FFprobe (ffprobe.exe):", "Automatic")
        self.ytdlp = self._file_row(advanced, "yt-dlp (yt-dlp.exe):", "Built-in yt-dlp")
        advanced.addRow("", hint("An external yt-dlp.exe can be updated at any time with 'yt-dlp -U' when a website "
                                 "stops working, without waiting for a new version of this app."))
        tools_row = QHBoxLayout()
        for text, target in (("Open log folder", paths.log_dir), ("Open settings folder", paths.data_dir)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, t=target: open_path(t()))
            tools_row.addWidget(button)
        diagnostics = QPushButton("Diagnostics...")
        diagnostics.clicked.connect(lambda: self.ctx.navigate("diagnostics"))
        tools_row.addWidget(diagnostics)
        tools_row.addStretch(1)
        advanced.addRow("", self._wrap(tools_row))

        buttons = QHBoxLayout()
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        revert = QPushButton("Undo changes")
        revert.clicked.connect(self.load)
        save = primary_button("Save settings")
        save.clicked.connect(self._save)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(revert)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.load()

    # -- layout helpers ------------------------------------------------------
    @staticmethod
    def _group(layout, title: str) -> QFormLayout:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addWidget(box)
        return form

    @staticmethod
    def _wrap(layout) -> QWidget:
        widget = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        return widget

    def _folder_row(self, form: QFormLayout, text: str, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._pick_folder(edit))
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(browse)
        form.addRow(text, self._wrap(row))
        return edit

    def _file_row(self, form: QFormLayout, text: str, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._pick_file(edit))
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(browse)
        form.addRow(text, self._wrap(row))
        return edit

    def _pick_folder(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text() or str(Path.home()))
        if folder:
            edit.setText(str(Path(folder)))

    def _pick_file(self, edit: QLineEdit) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "Choose the program", edit.text() or "C:\\",
                                              "Programs (*.exe);;All files (*.*)")
        if file:
            edit.setText(str(Path(file)))

    # -- data ------------------------------------------------------------------
    def load(self) -> None:
        s = self.ctx.settings
        self.download_dir.setText(s.download_dir)
        self.output_dir.setText(s.output_dir)
        self.remember_dir.setChecked(s.remember_last_dir)
        self._select(self.overwrite, s.overwrite_policy)
        self.open_after.setChecked(s.open_folder_after)
        self._select(self.theme, s.theme)
        self._select(self.container, s.video_container)
        self._select(self.quality, s.video_quality)
        self.add_audio.setChecked(s.video_add_audio)
        self.embed_metadata.setChecked(s.embed_metadata)
        self._select(self.audio_format, s.audio_format)
        self._select(self.audio_bitrate, s.audio_bitrate)
        self.pdf_name.setText(s.pdf_default_name)
        self.pdf_dpi.setValue(s.pdf_dpi)
        self._select(self.translation_target, s.translation_target)
        self.check_updates.setChecked(s.check_updates)
        self.ffmpeg.setText(s.ffmpeg_path)
        self.ffprobe.setText(s.ffprobe_path)
        self.ytdlp.setText(s.ytdlp_path)

    @staticmethod
    def _select(box, value) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def _collect(self) -> Settings:
        values = asdict(self.ctx.settings)
        values.update(
            download_dir=self.download_dir.text().strip().strip('"'),
            output_dir=self.output_dir.text().strip().strip('"'),
            remember_last_dir=self.remember_dir.isChecked(),
            overwrite_policy=self.overwrite.currentData(),
            open_folder_after=self.open_after.isChecked(),
            theme=self.theme.currentData(),
            video_container=self.container.currentData(),
            video_quality=self.quality.currentData(),
            video_add_audio=self.add_audio.isChecked(),
            embed_metadata=self.embed_metadata.isChecked(),
            audio_format=self.audio_format.currentData(),
            audio_bitrate=self.audio_bitrate.currentData(),
            pdf_default_name=self.pdf_name.text().strip() or "combined",
            pdf_dpi=self.pdf_dpi.value(),
            translation_target=self.translation_target.currentData(),
            check_updates=self.check_updates.isChecked(),
            ffmpeg_path=self.ffmpeg.text().strip().strip('"'),
            ffprobe_path=self.ffprobe.text().strip().strip('"'),
            ytdlp_path=self.ytdlp.text().strip().strip('"'),
        )
        return Settings(**values)

    def _save(self) -> None:
        new = self._collect()
        problems = []
        for label_text, value in (("Download folder", new.download_dir), ("Default output folder", new.output_dir)):
            if value and not Path(value).is_absolute():
                problems.append(f"{label_text}: please enter a complete path (for example C:\\Users\\You\\Videos).")
        for label_text, value in (("FFmpeg", new.ffmpeg_path), ("FFprobe", new.ffprobe_path), ("yt-dlp", new.ytdlp_path)):
            if value and not Path(value).exists():
                problems.append(f"{label_text}: the file does not exist ({value}).")
        if problems:
            show_info(self, "Please check the settings", "\n".join(problems))
            return
        self.ctx.replace_settings(new)
        show_info(self, "Settings saved", "Your settings were saved.")

    def _reset(self) -> None:
        if confirm(self, "Reset settings", "Reset all settings to their defaults?", "Reset"):
            geometry = self.ctx.settings.window_geometry
            fresh = Settings(window_geometry=geometry)
            self.ctx.replace_settings(fresh)
            self.load()

    def _clear_translation_progress(self) -> None:
        if confirm(self, "Clear translation progress",
                   "Forget the pages that were already translated by runs that did not finish?\n\n"
                   "Running those translations again will then start from the beginning (and use your API quota "
                   "again).", "Clear"):
            from app.services.translation.job import clear_cache

            clear_cache()
            show_info(self, "Translation progress cleared", "Saved translation progress was removed.")

    def _check_updates_now(self) -> None:
        window = self.ctx.window
        if window is None or not hasattr(window, "check_for_updates"):
            return
        self.check_now.setEnabled(False)
        self.check_now.setText("Checking...")

        def finished(_info) -> None:
            self.check_now.setEnabled(True)
            self.check_now.setText("Check for updates now")

        window.check_for_updates(manual=True, on_finished=finished)
