"""Safe Windows file names and output-path helpers."""

from __future__ import annotations

import re
import secrets
from pathlib import Path

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{c}" for c in "123456789¹²³"}
    | {f"LPT{c}" for c in "123456789¹²³"}
)
MAX_NAME_LENGTH = 150  # keeps full paths comfortably below the classic 260 limit
PARTIAL_MARKER = ".mt-partial-"


def sanitize_filename(name: str, fallback: str = "output", max_length: int = MAX_NAME_LENGTH) -> str:
    """Turn arbitrary text (e.g. a video title) into a valid Windows file name.

    Directory separators are replaced too, so the result can never escape the
    chosen output folder.
    """
    text = re.sub(r"[\t\n\r\f\v]+", " ", str(name))  # line breaks in titles become spaces
    text = _INVALID_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[:max_length]
    # Windows silently drops trailing dots and spaces, which would change the name.
    text = text.rstrip(". ")
    if not text:
        text = fallback
    if text.split(".")[0].strip().upper() in _RESERVED_NAMES:
        text = f"_{text}"
    return text


def normalize_extension(ext: str) -> str:
    """'MP4' / '.mp4' -> 'mp4'."""
    return ext.strip().lstrip(".").lower()


def strip_known_extension(stem: str, ext: str) -> str:
    """If the user typed 'clip.mp4' while the extension is fixed to mp4,
    drop the duplicate extension."""
    ext = normalize_extension(ext)
    if ext and stem.lower().endswith("." + ext):
        return stem[: -(len(ext) + 1)]
    return stem


def build_filename(stem: str, ext: str, fallback: str = "output") -> str:
    """Sanitised ``stem.ext``."""
    ext = normalize_extension(ext)
    stem = sanitize_filename(strip_known_extension(stem, ext), fallback=fallback)
    return f"{stem}.{ext}" if ext else stem


def unique_path(path: Path) -> Path:
    """``name.ext`` -> ``name (1).ext`` ... until the name is free."""
    if not path.exists():
        return path
    for number in range(1, 10_000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free file name for {path}")


def unique_paths(paths: list[Path]) -> list[Path]:
    """Like :func:`unique_path` for a batch, also avoiding clashes inside the batch."""
    taken: set[str] = set()
    result = []
    for path in paths:
        candidate = path
        number = 0
        while candidate.exists() or str(candidate).lower() in taken:
            number += 1
            candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        taken.add(str(candidate).lower())
        result.append(candidate)
    return result


def temp_sibling(path: Path) -> Path:
    """A temporary file next to ``path`` that keeps its extension (FFmpeg picks
    the container from the extension). Outputs are written here first and only
    renamed to the final name after they were verified."""
    return path.with_name(f"{path.stem}{PARTIAL_MARKER}{secrets.token_hex(3)}{path.suffix}")


def numbered_names(base: str, count: int, ext: str, label: str = "part", min_width: int = 2) -> list[str]:
    """``document_part01.pdf`` style names for multi-file outputs."""
    width = max(min_width, len(str(count)))
    base = sanitize_filename(base)
    ext = normalize_extension(ext)
    return [f"{base}_{label}{i:0{width}d}.{ext}" for i in range(1, count + 1)]


def same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()
