"""Read media information with ffprobe."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from app.core.errors import InvalidInputError, ProcessingError
from app.models.media import MediaInfo, parse_ffprobe_json
from app.utils.system import hidden_subprocess_kwargs

log = logging.getLogger(__name__)


def probe(ffprobe: Path, path: Path, timeout: float = 60) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise InvalidInputError(f"The file could not be found:\n{path}")
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError(f"Reading information from '{path.name}' took too long.") from exc
    except OSError as exc:
        raise ProcessingError("FFprobe could not be started.", details=str(exc)) from exc
    if result.returncode != 0:
        details = (result.stderr or "").strip()
        log.warning("ffprobe failed for %s: %s", path, details)
        raise InvalidInputError(
            f"'{path.name}' could not be read as a media file. It may be damaged or in an unsupported format.",
            details=details[-2000:],
        )
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise ProcessingError("FFprobe returned unreadable information.", details=str(exc)) from exc
    info = parse_ffprobe_json(data, path)
    if not info.streams:
        raise InvalidInputError(f"'{path.name}' does not contain any audio or video.")
    return info
