"""Subtitle files for "Burn in subtitles".

The user's subtitle file (SRT, ASS/SSA or WebVTT) is read in Python, its text
encoding is detected (or chosen by the user) and a UTF-8 copy with a plain
name is written to a private temporary folder. FFmpeg then runs with that
folder as its working directory and reads the copy by its relative name, so
unusual characters in the user's paths never need filter escaping. The
user's own subtitle file is only ever read.
"""

from __future__ import annotations

import logging
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import InvalidInputError
from app.utils.system import hidden_subprocess_kwargs

log = logging.getLogger(__name__)

SUBTITLE_EXTENSIONS = ("srt", "ass", "ssa", "vtt")
STYLED_EXTENSIONS = ("ass", "ssa")  # formats that carry their own fonts, colours and positions
MAX_SUBTITLE_BYTES = 20 * 1024 * 1024
MAX_OFFSET_SECONDS = 3600.0

ENCODINGS: dict[str, str] = {
    "auto": "Automatic",
    "utf-8": "UTF-8 (Unicode)",
    "utf-16": "UTF-16 (Unicode)",
    "cp1252": "Western European (Windows-1252)",
    "cp1250": "Central European (Windows-1250)",
    "cp1251": "Cyrillic (Windows-1251)",
    "cp1253": "Greek (Windows-1253)",
    "cp1254": "Turkish (Windows-1254)",
    "cp1255": "Hebrew (Windows-1255)",
    "cp1256": "Arabic (Windows-1256)",
    "cp874": "Thai (Windows-874)",
    "gb18030": "Chinese Simplified (GB18030)",
    "big5": "Chinese Traditional (Big5)",
    "shift_jis": "Japanese (Shift-JIS)",
    "euc_kr": "Korean (EUC-KR)",
}

# Tie-break for automatic detection: the legacy code pages subtitle files most often use.
_PREFERRED = ("cp1252", "cp1251", "cp1250", "cp1256", "cp1253", "cp1254", "cp1255", "cp874", "gb18030", "big5",
              "shift_jis", "euc_kr")

# Sizes are in libass units for plain subtitles (the script is 288 units high),
# so the text keeps the same proportion at any video resolution.
SIZES: dict[str, tuple[str, int]] = {
    "small": ("Small", 14),
    "medium": ("Medium", 18),
    "large": ("Large", 22),
    "xlarge": ("Extra large", 28),
}
COLORS: dict[str, tuple[str, str]] = {  # ASS colours are &HAABBGGRR
    "white": ("White", "&H00FFFFFF"),
    "yellow": ("Yellow", "&H0000FFFF"),
}
BACKGROUNDS: dict[str, str] = {
    "outline": "Black outline around the letters",
    "box": "Dark box behind the text",
}
# FFmpeg applies these overrides with the classic SSA alignment numbers: 2 = bottom centre, 6 = top centre.
POSITIONS: dict[str, tuple[str, int]] = {
    "bottom": ("Bottom", 2),
    "top": ("Top", 6),
}


@dataclass(frozen=True)
class SubtitleStyle:
    size: str = "medium"
    color: str = "white"
    background: str = "outline"
    position: str = "bottom"
    keep_file_style: bool = True  # for ASS/SSA files: keep the file's own styling

    def validate(self) -> None:
        for value, allowed, what in ((self.size, SIZES, "size"), (self.color, COLORS, "colour"),
                                     (self.background, BACKGROUNDS, "background"),
                                     (self.position, POSITIONS, "position")):
            if value not in allowed:
                raise InvalidInputError(f"Unknown subtitle {what}: {value}")

    def force_style(self) -> str:
        """libass style overrides, e.g. 'FontSize=18,PrimaryColour=&H00FFFFFF,...'."""
        self.validate()
        size = SIZES[self.size][1]
        # Encoding=-1 makes libass detect the text direction of each line, so right-to-left subtitles
        # (Arabic, Hebrew, Persian, Urdu) get their punctuation on the correct side.
        parts = [f"FontSize={size}", f"PrimaryColour={COLORS[self.color][1]}", "Bold=0",
                 f"Alignment={POSITIONS[self.position][1]}", f"MarginV={max(10, round(size * 0.9))}", "Encoding=-1"]
        if self.background == "box":
            parts += ["BorderStyle=3", "Outline=1.2", "Shadow=0", "OutlineColour=&H80000000", "BackColour=&H80000000"]
        else:
            outline = round(size / 12, 1)
            parts += ["BorderStyle=1", f"Outline={outline}", f"Shadow={round(outline / 2, 1)}",
                      "OutlineColour=&H00000000", "BackColour=&H80000000"]
        return ",".join(parts)


def subtitle_ext(path: Path) -> str:
    return Path(path).suffix.lower().lstrip(".")


def find_matching_subtitles(video: Path) -> Path | None:
    """A subtitle file next to the video with the same name, e.g. 'Movie.srt'
    or 'Movie.en.srt' for 'Movie.mp4' (exact name first)."""
    video = Path(video)
    try:
        candidates = [p for p in video.parent.iterdir() if p.is_file() and subtitle_ext(p) in SUBTITLE_EXTENSIONS]
    except OSError:
        return None
    stem = video.stem.lower()
    exact = [p for p in candidates if p.stem.lower() == stem]
    if exact:
        return sorted(exact, key=lambda p: SUBTITLE_EXTENSIONS.index(subtitle_ext(p)))[0]
    tagged = sorted(p for p in candidates if p.stem.lower().startswith(stem + "."))
    return tagged[0] if tagged else None


def decode_subtitle(data: bytes, encoding: str = "auto") -> tuple[str, str]:
    """Bytes of a subtitle file -> (text, encoding used)."""
    if encoding not in ENCODINGS:
        raise InvalidInputError(f"Unknown text encoding: {encoding}")
    if encoding != "auto":
        try:
            return data.decode("utf-8-sig" if encoding == "utf-8" else encoding), encoding
        except (UnicodeDecodeError, LookupError) as exc:
            raise InvalidInputError(f"The subtitle file is not in {ENCODINGS[encoding]}. Choose another text "
                                    "encoding, or 'Automatic'.") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes  # installed with requests

        matches = list(from_bytes(data))
    except Exception:  # noqa: BLE001 - detection is best effort
        log.debug("Encoding detection failed", exc_info=True)
        matches = []
    western = _decode_western(data)
    if matches:
        top = matches[0]
        if top.coherence == 0 and western is not None:
            return western, "cp1252"  # no language recognised, and it reads as normal Western text
        # Text often fits several code pages equally well (French in Windows-1250 and 1252, for
        # example); then prefer the one most subtitle files actually use.
        ties = [m for m in matches if m.chaos == top.chaos and m.coherence == top.coherence]
        best = min(ties, key=lambda m: _PREFERRED.index(m.encoding) if m.encoding in _PREFERRED else len(_PREFERRED))
        return str(best), best.encoding
    return data.decode("cp1252", errors="replace"), "cp1252"


def _decode_western(data: bytes) -> str | None:
    """The text as Windows-1252 if it looks like normal Western European text:
    mostly plain letters with some accents, and no symbols such as '³' or '¹'
    stuck inside words (which is what other code pages look like when read as
    Windows-1252). None otherwise."""
    try:
        text = data.decode("cp1252")
    except UnicodeDecodeError:
        return None
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(1 for c in letters if ord(c) > 127) / len(letters) > 0.3:
        return None
    for before, char, after in zip(text, text[1:], text[2:], strict=False):
        if ord(char) > 127 and before.isalpha() and after.isalpha() and unicodedata.category(char)[0] in "NS":
            return None
    return text


def encoding_label(encoding: str) -> str:
    return ENCODINGS.get(encoding, encoding)


def prepare_subtitles(source: Path, folder: Path, encoding: str = "auto") -> tuple[Path, str]:
    """Write a UTF-8 copy of ``source`` into ``folder`` as 'subtitles.<ext>'.
    Returns (copy, encoding that was used to read the original)."""
    source = Path(source)
    ext = subtitle_ext(source)
    if ext not in SUBTITLE_EXTENSIONS:
        raise InvalidInputError(f"'{source.name}' is not a supported subtitle file. Use SRT, ASS, SSA or VTT.")
    try:
        size = source.stat().st_size
        if size > MAX_SUBTITLE_BYTES:
            raise InvalidInputError(f"'{source.name}' is too large to be a subtitle file.")
        data = source.read_bytes()
    except OSError as exc:
        raise InvalidInputError(f"The subtitle file could not be read:\n{source}", details=str(exc)) from exc
    text, used = decode_subtitle(data, encoding)
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.strip():
        raise InvalidInputError(f"'{source.name}' is empty.")
    copy = Path(folder) / f"subtitles.{ext}"
    copy.write_text(text, encoding="utf-8", newline="\n")
    return copy, used


def cue_start_times(ffprobe: Path, subtitle: Path, timeout: float = 60) -> list[float]:
    """Start time (seconds) of every subtitle in the file, as FFmpeg reads it."""
    subtitle = Path(subtitle)
    try:
        result = subprocess.run(
            [str(ffprobe), "-v", "error", "-select_streams", "s:0", "-show_entries", "packet=pts_time",
             "-of", "csv=p=0", subtitle.name],
            cwd=str(subtitle.parent), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, **hidden_subprocess_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidInputError("The subtitle file could not be read.", details=str(exc)) from exc
    times = []
    for line in result.stdout.splitlines():
        try:
            times.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    if result.returncode != 0 and not times:
        log.info("ffprobe could not read subtitles: %s", result.stderr.strip()[-500:])
    return sorted(times)
