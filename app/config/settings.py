"""User settings, persisted as JSON.

Loading never fails: unknown keys are ignored, invalid values fall back to
their defaults, and a corrupt file is backed up and replaced by defaults.
Saving is atomic (write a temp file, then rename), so a crash cannot leave a
half-written settings file behind.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from app.config import paths
from app.services.translation.languages import LANGUAGES

log = logging.getLogger(__name__)

OVERWRITE_ASK = "ask"
OVERWRITE_RENAME = "rename"
OVERWRITE_REPLACE = "replace"

CHOICES: dict[str, tuple] = {
    "overwrite_policy": (OVERWRITE_ASK, OVERWRITE_RENAME, OVERWRITE_REPLACE),
    "theme": ("system", "light", "dark"),
    "video_container": ("auto", "mp4", "mkv", "webm"),
    "video_quality": ("best", "2160", "1440", "1080", "720", "480", "360"),
    "audio_format": ("original", "mp3", "m4a", "wav", "flac", "opus"),
    "audio_bitrate": (96, 128, 160, 192, 256, 320),
    "translation_target": tuple(lang.code for lang in LANGUAGES),
    "translation_source": ("",) + tuple(lang.code for lang in LANGUAGES),  # "" = detect automatically
    "translation_doc_type": ("auto", "text", "comic", "scan"),
}
RANGES: dict[str, tuple[int, int]] = {"pdf_dpi": (36, 600)}


@dataclass
class Settings:
    # General
    download_dir: str = ""  # empty = the user's Downloads folder
    output_dir: str = ""  # empty = the folder of the input file
    remember_last_dir: bool = True
    last_dir: str = ""
    overwrite_policy: str = OVERWRITE_ASK
    open_folder_after: bool = False
    theme: str = "system"
    # Video
    video_container: str = "auto"
    video_quality: str = "best"
    video_add_audio: bool = True  # pair video-only formats with the best audio
    embed_metadata: bool = True  # write title/artist tags into downloads
    # Audio
    audio_format: str = "mp3"
    audio_bitrate: int = 192
    # PDF
    pdf_default_name: str = "combined"
    pdf_dpi: int = 150
    # AI translation (the API key itself is never stored here - see app/config/credentials.py)
    translation_target: str = "en"
    translation_source: str = ""
    translation_doc_type: str = "auto"
    translation_model: str = ""  # empty = the provider's recommended model
    translation_sfx: bool = True  # also translate sound effects in comics (as small labels)
    # Updates
    check_updates: bool = True  # ask GitHub for a newer version when the app starts
    update_skipped_version: str = ""  # "Skip this version" was chosen for this version
    # Advanced
    ffmpeg_path: str = ""  # empty = automatic (bundled copy, then PATH)
    ffprobe_path: str = ""
    ytdlp_path: str = ""  # empty = the yt-dlp library built into the app
    # Window state
    window_geometry: str = ""

    def effective_download_dir(self) -> Path:
        if self.download_dir:
            return Path(self.download_dir)
        return paths.downloads_dir()

    def output_dir_for(self, input_path: Path | None) -> Path:
        """Default output folder: the configured one, else next to the input."""
        if self.output_dir:
            return Path(self.output_dir)
        if input_path is not None:
            return Path(input_path).parent
        return self.effective_download_dir()

    def copy(self) -> Settings:
        return Settings(**asdict(self))


def _coerce(name: str, value: object, default: object) -> object:
    """Validate one loaded value against the type/choices of its default."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ValueError
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        number = int(value)
        low, high = RANGES.get(name, (None, None))
        if low is not None and not (low <= number <= high):
            raise ValueError
        if name in CHOICES and number not in CHOICES[name]:
            raise ValueError
        return number
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError
        if name in CHOICES and value not in CHOICES[name]:
            raise ValueError
        return value
    return value


def settings_from_dict(data: dict) -> Settings:
    defaults = Settings()
    values = {}
    for f in fields(Settings):
        default = getattr(defaults, f.name)
        if f.name not in data:
            continue
        try:
            values[f.name] = _coerce(f.name, data[f.name], default)
        except (ValueError, TypeError):
            log.warning("Ignoring invalid setting %s=%r", f.name, data[f.name])
    return Settings(**values)


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.settings_path()

    def load(self) -> Settings:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Settings()
        except OSError:
            log.warning("Could not read settings file %s", self.path, exc_info=True)
            return Settings()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("settings root is not an object")
        except ValueError:
            backup = self.path.with_suffix(".corrupt.json")
            log.warning("Settings file is corrupt; keeping a copy at %s and using defaults", backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return Settings()
        return settings_from_dict(data)

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), indent=2, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
