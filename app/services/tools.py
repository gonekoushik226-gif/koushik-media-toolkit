"""Locating external programs: FFmpeg, FFprobe, yt-dlp.exe, JavaScript runtimes.

Search order for FFmpeg/FFprobe (first match wins):
  1. The path configured in Settings -> Advanced (file or folder).
  2. The copy bundled with the application (``tools`` folder in the bundle).
  3. A ``tools`` folder or the EXE folder next to the running EXE (lets users
     of the portable EXE drop newer binaries next to it).
  4. The system PATH - a deliberate, clearly reported fallback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import paths
from app.core.errors import DependencyError
from app.utils.system import hidden_subprocess_kwargs

SOURCE_SETTINGS = "Settings"
SOURCE_BUNDLED = "bundled with the app"
SOURCE_NEXT_TO_APP = "next to the app"
SOURCE_PATH = "system PATH"
SOURCE_MISSING = "not found"


@dataclass(frozen=True)
class ToolInfo:
    name: str
    path: Path | None
    source: str
    configured_but_missing: str = ""  # a Settings path that does not exist

    @property
    def found(self) -> bool:
        return self.path is not None


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: Path
    ffprobe: Path


def _exe_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _configured_candidate(configured: str, name: str) -> Path | None:
    if not configured:
        return None
    path = Path(os.path.expandvars(configured.strip().strip('"')))
    if path.is_dir():
        path = path / _exe_name(name)
    return path if path.is_file() else None


def find_tool(name: str, configured: str = "") -> ToolInfo:
    exe = _exe_name(name)
    configured_path = _configured_candidate(configured, name)
    if configured_path:
        return ToolInfo(name, configured_path, SOURCE_SETTINGS)
    missing = configured.strip() if configured.strip() else ""

    bundled = paths.bundled_tools_dir() / exe
    if bundled.is_file():
        return ToolInfo(name, bundled, SOURCE_BUNDLED, missing)
    exe_dir = paths.executable_dir()
    for candidate in (exe_dir / "tools" / exe, exe_dir / exe):
        if candidate.is_file():
            return ToolInfo(name, candidate, SOURCE_NEXT_TO_APP, missing)
    on_path = shutil.which(name)
    if on_path:
        return ToolInfo(name, Path(on_path), SOURCE_PATH, missing)
    return ToolInfo(name, None, SOURCE_MISSING, missing)


_JS_RUNTIME_CANDIDATES: dict[str, tuple[str, ...]] = {
    # name used by yt-dlp -> executable / well-known install locations
    "deno": ("deno", r"%USERPROFILE%\.deno\bin\deno.exe"),
    "node": ("node", r"%ProgramFiles%\nodejs\node.exe"),
    "bun": ("bun", r"%USERPROFILE%\.bun\bin\bun.exe"),
    "quickjs": ("qjs",),
}


def find_js_runtimes() -> dict[str, Path]:
    """JavaScript runtimes yt-dlp can use for YouTube's player challenges.
    Deno first because yt-dlp recommends it."""
    found: dict[str, Path] = {}
    for runtime, candidates in _JS_RUNTIME_CANDIDATES.items():
        for candidate in candidates:
            if "\\" in candidate:
                path = Path(os.path.expandvars(candidate))
                if path.is_file():
                    found[runtime] = path
                    break
            elif located := shutil.which(candidate):
                found[runtime] = Path(located)
                break
    return found


def tool_version(path: Path, args: tuple[str, ...] = ("-version",), timeout: float = 15) -> str:
    """First line of ``<tool> -version`` (or '' if it cannot be run)."""
    try:
        result = subprocess.run(
            [str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0].strip() if output else ""


class ToolLocator:
    """Finds tools using the *current* settings each time it is asked, so a
    change in Settings takes effect without restarting."""

    def __init__(self, settings_getter: Callable[[], object]):
        self._settings = settings_getter

    def ffmpeg(self) -> ToolInfo:
        return find_tool("ffmpeg", getattr(self._settings(), "ffmpeg_path", ""))

    def ffprobe(self) -> ToolInfo:
        return find_tool("ffprobe", getattr(self._settings(), "ffprobe_path", ""))

    def ytdlp_executable(self) -> ToolInfo | None:
        """External yt-dlp.exe if one is configured; ``None`` = built-in library."""
        configured = getattr(self._settings(), "ytdlp_path", "")
        if not configured:
            return None
        path = _configured_candidate(configured, "yt-dlp")
        if path:
            return ToolInfo("yt-dlp", path, SOURCE_SETTINGS)
        return ToolInfo("yt-dlp", None, SOURCE_MISSING, configured)

    def media_tools(self) -> MediaTools:
        """FFmpeg + FFprobe, or a friendly :class:`DependencyError`."""
        ffmpeg, ffprobe = self.ffmpeg(), self.ffprobe()
        missing = [{"ffmpeg": "FFmpeg", "ffprobe": "FFprobe"}[t.name] for t in (ffmpeg, ffprobe) if not t.found]
        if missing:
            raise DependencyError(
                f"{' and '.join(missing)} could not be found, so video and audio features are unavailable.\n\n"
                "The installer and portable EXE include FFmpeg. If it went missing, reinstall the "
                "application, or install FFmpeg (for example: winget install Gyan.FFmpeg) and set its "
                "location in Settings > Advanced. Open Diagnostics for details."
            )
        return MediaTools(ffmpeg.path, ffprobe.path)  # type: ignore[arg-type]
