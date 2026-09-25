"""Operating-system helpers: hidden subprocess windows, opening folders,
terminating process trees."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


def hidden_subprocess_kwargs() -> dict:
    """Keyword arguments that stop console windows flashing up when the GUI
    (a windowed EXE) starts FFmpeg or yt-dlp."""
    if not IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startupinfo}


def open_path(path: Path) -> bool:
    """Open a folder or file with its default program. Never raises."""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # noqa: S606 - opening a local path chosen by the user
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except OSError:
        log.warning("Could not open %s", path, exc_info=True)
        return False


def reveal_in_explorer(path: Path) -> bool:
    """Open the containing folder with the file selected (Windows), or just
    open the folder elsewhere / when the file is gone."""
    path = Path(path)
    if IS_WINDOWS and path.is_file():
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
            return True
        except OSError:
            log.warning("Could not reveal %s", path, exc_info=True)
    folder = path if path.is_dir() else path.parent
    return open_path(folder)


def kill_process_tree(process: subprocess.Popen) -> None:
    """Terminate a process and its children (yt-dlp spawns FFmpeg)."""
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            log.debug("taskkill failed", exc_info=True)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
