"""Objects shared by all pages (settings, job controller, tool locator)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtWidgets import QWidget

from app.config.settings import Settings, SettingsStore
from app.services.audio import AudioService
from app.services.tools import ToolLocator
from app.services.video import VideoService

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    store: SettingsStore
    tools: ToolLocator
    window: QWidget | None = None
    jobs: object | None = None  # JobController (set by the main window)
    navigate: Callable[..., None] = lambda key, operation=None: None
    api_keys: object | None = None  # ApiKeyManager (created on first use)
    # (api_key, model) -> TranslationProvider; tests replace it with a fake provider
    provider_factory: Callable[[str, str], object] | None = None
    _settings_listeners: list[Callable[[], None]] = field(default_factory=list)

    # -- settings ------------------------------------------------------------
    def save_settings(self) -> None:
        try:
            self.store.save(self.settings)
        except OSError:
            log.warning("Could not save settings", exc_info=True)

    def replace_settings(self, new: Settings) -> None:
        self.settings = new
        self.save_settings()
        for listener in list(self._settings_listeners):
            listener()

    def on_settings_changed(self, listener: Callable[[], None]) -> None:
        self._settings_listeners.append(listener)

    # -- folders -------------------------------------------------------------
    def start_dir(self, fallback: Path | None = None) -> str:
        """Initial folder for file dialogs."""
        if self.settings.remember_last_dir and self.settings.last_dir and Path(self.settings.last_dir).is_dir():
            return self.settings.last_dir
        if fallback is not None and Path(fallback).is_dir():
            return str(fallback)
        return str(Path.home())

    def remember_dir(self, path: Path | str) -> None:
        if not self.settings.remember_last_dir:
            return
        folder = Path(path)
        folder = folder if folder.is_dir() else folder.parent
        if str(folder) != self.settings.last_dir:
            self.settings.last_dir = str(folder)
            self.save_settings()

    def output_dir_for(self, input_path: Path | None) -> Path:
        return self.settings.output_dir_for(input_path)

    # -- services --------------------------------------------------------------
    def video_service(self) -> VideoService:
        return VideoService(self.tools.media_tools())

    def audio_service(self) -> AudioService:
        return AudioService(self.tools.media_tools())

    # -- AI translation --------------------------------------------------------
    def key_manager(self):
        """The user's own API key (Windows Credential Manager)."""
        if self.api_keys is None:
            from app.services.translation.keys import ApiKeyManager

            self.api_keys = ApiKeyManager()
        return self.api_keys

    def translation_model(self) -> str:
        from app.services.translation.gemini import DEFAULT_MODEL

        return self.settings.translation_model or DEFAULT_MODEL

    def translation_provider(self, api_key: str | None = None, model: str | None = None):
        """A provider using the user's key (raises MissingApiKeyError if there is none)."""
        from app.services.translation.provider import MissingApiKeyError

        key = api_key or self.key_manager().key()
        if not key:
            raise MissingApiKeyError("No API key is configured for AI translation.")
        model = model or self.translation_model()
        if self.provider_factory is not None:
            return self.provider_factory(key, model)
        from app.services.translation.gemini import GeminiProvider

        return GeminiProvider(key, model)
