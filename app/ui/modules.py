"""Registration of the built-in modules.

To add a module (for example "Subtitles"): write a page widget (usually an
``OperationsPage`` with a few ``OperationPanel`` classes) and add one
``ModuleSpec`` below. See docs/DEVELOPER_GUIDE.md.
"""

from __future__ import annotations

from app.core.registry import GROUP_MAIN, GROUP_UTILITY, ModuleRegistry, ModuleSpec


def _video(ctx):
    from app.ui.pages.video import build_video_page

    return build_video_page(ctx)


def _audio(ctx):
    from app.ui.pages.audio import build_audio_page

    return build_audio_page(ctx)


def _images(ctx):
    from app.ui.pages.images import build_images_page

    return build_images_page(ctx)


def _pdf(ctx):
    from app.ui.pages.pdf import build_pdf_page

    return build_pdf_page(ctx)


def _translate(ctx):
    from app.ui.pages.translate import build_translate_page

    return build_translate_page(ctx)


def _download(ctx):
    from app.ui.pages.downloads import DownloadHubPage

    return DownloadHubPage(ctx)


def _settings(ctx):
    from app.ui.pages.settings import SettingsPage

    return SettingsPage(ctx)


def _diagnostics(ctx):
    from app.ui.pages.diagnostics import DiagnosticsPage

    return DiagnosticsPage(ctx)


def build_registry() -> ModuleRegistry:
    registry = ModuleRegistry()
    for spec in (
        ModuleSpec("video", "VIDEO", "Download from websites, trim, merge, convert, resize, rotate, change speed or "
                   "volume, compress, burn in subtitles.", "video", _video, GROUP_MAIN, 10),
        ModuleSpec("audio", "AUDIO / MUSIC", "Download music, convert to MP3 and more, trim, merge, fade, "
                   "change volume, edit tags.", "audio", _audio, GROUP_MAIN, 20),
        ModuleSpec("images", "IMAGES", "Crop, resize, rotate, adjust, convert and reorder images, or turn them "
                   "into a PDF.", "image", _images, GROUP_MAIN, 30),
        ModuleSpec("pdf", "PDF", "Create PDFs from images, merge, split, download, or save pages as images.",
                   "pdf", _pdf, GROUP_MAIN, 40),
        ModuleSpec("translate", "TRANSLATE", "Translate PDFs, books, scans, manga, comics and webtoons with AI, "
                   "using your own API key.", "translate", _translate, GROUP_MAIN, 45),
        ModuleSpec("download", "DOWNLOAD", "Download video, audio or a PDF from a link.", "download", _download,
                   GROUP_UTILITY, 50),
        ModuleSpec("settings", "SETTINGS", "Folders, preferred formats, theme and advanced options.", "settings",
                   _settings, GROUP_UTILITY, 60),
        ModuleSpec("diagnostics", "DIAGNOSTICS", "Check that FFmpeg, yt-dlp and the other components work.",
                   "diagnostics", _diagnostics, GROUP_UTILITY, 70),
    ):
        registry.register(spec)
    return registry
