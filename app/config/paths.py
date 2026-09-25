"""Filesystem locations used by the application.

Everything that depends on whether we run from source or from a PyInstaller
bundle is decided here, so the rest of the code never has to check.

Data locations:
  * Normal install: settings in %APPDATA%\\KoushikMediaToolkit,
    logs in %LOCALAPPDATA%\\KoushikMediaToolkit\\logs.
  * Portable mode: if a file named ``portable.txt`` or a folder named
    ``KoushikMediaToolkit-data`` sits next to the EXE, everything is kept
    in that folder instead.
  * ``KMT_DATA_DIR`` (environment variable) overrides both; used by tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app import APP_ID

PORTABLE_MARKER = "portable.txt"
PORTABLE_DATA_DIRNAME = f"{APP_ID}-data"
_DOWNLOADS_FOLDER_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def bundle_dir() -> Path:
    """Folder holding bundled read-only resources (``assets``, ``tools``)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return project_root()


def executable_dir() -> Path:
    """Folder containing the running EXE (or the project root from source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def asset_path(*parts: str) -> Path:
    return bundle_dir().joinpath("assets", *parts)


def bundled_tools_dir() -> Path:
    """Where the build places ffmpeg.exe / ffprobe.exe."""
    return bundle_dir() / "tools"


def portable_data_dir() -> Path | None:
    exe_dir = executable_dir()
    if (exe_dir / PORTABLE_MARKER).exists() or (exe_dir / PORTABLE_DATA_DIRNAME).is_dir():
        return exe_dir / PORTABLE_DATA_DIRNAME
    return None


def _env_dir(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def data_dir() -> Path:
    """Folder for settings.json (created on demand)."""
    override = os.environ.get("KMT_DATA_DIR")
    if override:
        base = Path(override)
    elif (portable := portable_data_dir()) is not None:
        base = portable
    else:
        base = _env_dir("APPDATA", Path.home() / "AppData" / "Roaming") / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_dir() -> Path:
    """Folder for log files (created on demand)."""
    override = os.environ.get("KMT_DATA_DIR")
    if override:
        folder = Path(override) / "logs"
    elif (portable := portable_data_dir()) is not None:
        folder = portable / "logs"
    else:
        folder = _env_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_ID / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def settings_path() -> Path:
    return data_dir() / "settings.json"


def downloads_dir() -> Path:
    """The user's Downloads folder, honouring folder redirection."""
    if sys.platform == "win32":
        try:
            found = _known_folder_path(_DOWNLOADS_FOLDER_GUID)
            if found:
                return Path(found)
        except (OSError, AttributeError, ValueError):
            pass
    return Path.home() / "Downloads"


def _known_folder_path(guid_text: str) -> str | None:
    import ctypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = _GUID()
    # oledll raises OSError automatically when the HRESULT signals failure.
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid_text), ctypes.byref(guid))
    buffer = ctypes.c_wchar_p()
    ctypes.oledll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buffer))
    try:
        return buffer.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buffer)
