import shutil

import pytest

from app.config.settings import Settings
from app.core.errors import DependencyError
from app.services import tools
from app.services.diagnostics import ERROR, OK, WARNING, run_diagnostics


def test_offline_diagnostics(tmp_path):
    settings = Settings(download_dir=str(tmp_path))
    checks = run_diagnostics(settings, tools.ToolLocator(lambda: settings), include_network=False, qt_version="6.11")
    by_name = {c.name: c for c in checks}
    for name in ("Application", "FFmpeg", "FFprobe", "yt-dlp", "Pillow (images)", "pypdf (PDF editing)",
                 "PyMuPDF (PDF rendering)", "Download folder", "Log folder"):
        assert name in by_name
    assert by_name["Download folder"].status in (OK, WARNING)
    assert all(c.status in ("ok", "warning", "error", "info") for c in checks)
    assert "Internet connection" not in by_name


def test_unwritable_folder_is_reported(tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    settings = Settings(download_dir=str(blocker / "sub"))  # cannot create a folder inside a file
    checks = run_diagnostics(settings, tools.ToolLocator(lambda: settings), include_network=False)
    assert next(c for c in checks if c.name == "Download folder").status == ERROR


def test_configured_tool_path(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg not installed")
    from pathlib import Path

    folder = str(Path(ffmpeg).parent)
    info = tools.find_tool("ffmpeg", folder)  # a folder is accepted as well as the exe path
    assert info.found and info.source == tools.SOURCE_SETTINGS


def test_missing_configured_path_falls_back_and_is_flagged(tmp_path):
    info = tools.find_tool("ffmpeg", str(tmp_path / "missing" / "ffmpeg.exe"))
    assert info.configured_but_missing


def test_missing_tools_raise_friendly_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools.paths, "bundled_tools_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(tools.paths, "executable_dir", lambda: tmp_path / "none")
    locator = tools.ToolLocator(lambda: Settings())
    assert not locator.ffmpeg().found
    with pytest.raises(DependencyError, match="FFmpeg and FFprobe could not be found"):
        locator.media_tools()


def test_external_ytdlp_setting(tmp_path):
    settings = Settings(ytdlp_path=str(tmp_path / "yt-dlp.exe"))
    external = tools.ToolLocator(lambda: settings).ytdlp_executable()
    assert external is not None and not external.found
    assert tools.ToolLocator(lambda: Settings()).ytdlp_executable() is None
