"""Online media and its downloadable formats, parsed from yt-dlp's info dict.

The same parser handles the embedded yt-dlp library and the external
``yt-dlp.exe -J`` JSON output, because both produce the same structure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

MANIFEST_PROTOCOLS = {"m3u8", "m3u8_native", "http_dash_segments", "http_dash_segments_generator", "f4m", "ism"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "opus", "ogg", "oga", "wav", "flac", "weba", "wma", "alac", "aiff"}

_CODEC_FAMILIES = (
    ("avc", "h264"),
    ("h264", "h264"),
    ("hev", "hevc"),
    ("hvc", "hevc"),
    ("h265", "hevc"),
    ("hevc", "hevc"),
    ("vp09", "vp9"),
    ("vp9", "vp9"),
    ("vp08", "vp8"),
    ("vp8", "vp8"),
    ("av01", "av1"),
    ("av1", "av1"),
    ("mp4v", "mpeg4"),
    ("mpeg4", "mpeg4"),
    ("mp4a.40.34", "mp3"),
    ("mp4a.6b", "mp3"),
    ("mp4a", "aac"),
    ("aac", "aac"),
    ("opus", "opus"),
    ("vorbis", "vorbis"),
    ("mp3", "mp3"),
    ("ac-3", "ac3"),
    ("ac3", "ac3"),
    ("ec-3", "eac3"),
    ("eac3", "eac3"),
    ("flac", "flac"),
    ("alac", "alac"),
    ("dts", "dts"),
    ("pcm", "pcm"),
)
_FRIENDLY = {
    "h264": "H.264",
    "hevc": "H.265",
    "vp9": "VP9",
    "vp8": "VP8",
    "av1": "AV1",
    "mpeg4": "MPEG-4",
    "aac": "AAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "mp3": "MP3",
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "flac": "FLAC",
    "alac": "ALAC",
    "dts": "DTS",
    "pcm": "PCM",
}


def codec_family(codec: str | None) -> str | None:
    """'avc1.640028' -> 'h264', 'vp09.00.40.08' -> 'vp9', 'mp4a.40.2' -> 'aac'."""
    if not codec or codec == "none":
        return None
    text = codec.strip().lower()
    for prefix, family in _CODEC_FAMILIES:
        if text.startswith(prefix):
            return family
    return text.split(".")[0]


def friendly_codec(codec: str | None) -> str:
    family = codec_family(codec)
    if family is None:
        return "-"
    return _FRIENDLY.get(family, codec or "-")


class FormatKind(StrEnum):
    COMBINED = "combined"
    VIDEO_ONLY = "video"
    AUDIO_ONLY = "audio"

    @property
    def label(self) -> str:
        return {"combined": "Video + audio", "video": "Video only", "audio": "Audio only"}[self.value]


def _num(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _int(value: object) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


@dataclass
class RemoteFormat:
    format_id: str
    ext: str
    kind: FormatKind
    kind_is_guess: bool = False  # the site did not report codecs
    vcodec: str | None = None
    acodec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    tbr: float | None = None
    vbr: float | None = None
    abr: float | None = None
    asr: int | None = None
    audio_channels: int | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    protocol: str = ""
    format_note: str = ""
    language: str | None = None
    language_preference: int = -1
    dynamic_range: str | None = None

    @property
    def has_video(self) -> bool:
        return self.kind in (FormatKind.COMBINED, FormatKind.VIDEO_ONLY)

    @property
    def has_audio(self) -> bool:
        return self.kind in (FormatKind.COMBINED, FormatKind.AUDIO_ONLY)

    @property
    def size_bytes(self) -> int | None:
        return self.filesize or self.filesize_approx

    @property
    def size_is_estimate(self) -> bool:
        return not self.filesize and bool(self.filesize_approx)

    @property
    def is_manifest(self) -> bool:
        """HLS/DASH streaming variants (often duplicates of direct formats)."""
        return any(p in MANIFEST_PROTOCOLS for p in self.protocol.split("+"))

    @property
    def is_drc(self) -> bool:
        return "drc" in self.format_note.lower() or self.format_id.lower().endswith("-drc")

    @property
    def video_family(self) -> str | None:
        return codec_family(self.vcodec)

    @property
    def audio_family(self) -> str | None:
        return codec_family(self.acodec)

    @property
    def resolution_text(self) -> str:
        if self.kind is FormatKind.AUDIO_ONLY:
            return "audio only"
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        if self.height:
            return f"{self.height}p"
        return "-"

    @property
    def audio_bitrate(self) -> float | None:
        if self.abr:
            return self.abr
        return self.tbr if self.kind is FormatKind.AUDIO_ONLY else None

    @property
    def total_bitrate(self) -> float | None:
        if self.tbr:
            return self.tbr
        if self.vbr or self.abr:
            return (self.vbr or 0) + (self.abr or 0)
        return None

    def short_label(self) -> str:
        """e.g. '1920x1080 60fps H.264' or 'Opus 129 kbps'."""
        if self.kind is FormatKind.AUDIO_ONLY:
            rate = f" {self.audio_bitrate:.0f} kbps" if self.audio_bitrate else ""
            return f"{friendly_codec(self.acodec) if self.acodec else self.ext.upper()}{rate}"
        fps = f" {self.fps:.0f}fps" if self.fps else ""
        codec = f" {friendly_codec(self.vcodec)}" if self.vcodec else ""
        return f"{self.resolution_text}{fps}{codec}"


@dataclass
class RemoteMedia:
    url: str
    title: str
    webpage_url: str = ""
    uploader: str = ""
    duration: float | None = None
    extractor: str = ""
    is_live: bool = False
    formats: list[RemoteFormat] = field(default_factory=list)
    default_format_ids: list[str] = field(default_factory=list)  # yt-dlp's own "best" choice
    drm_formats_skipped: int = 0

    def get(self, format_id: str) -> RemoteFormat | None:
        return next((f for f in self.formats if f.format_id == format_id), None)

    @property
    def video_formats(self) -> list[RemoteFormat]:
        items = [f for f in self.formats if f.has_video]
        return sorted(items, key=lambda f: (f.height or 0, f.fps or 0, f.total_bitrate or 0), reverse=True)

    @property
    def audio_formats(self) -> list[RemoteFormat]:
        items = [f for f in self.formats if f.kind is FormatKind.AUDIO_ONLY]
        return sorted(items, key=_audio_rank, reverse=True)

    @property
    def default_formats(self) -> list[RemoteFormat]:
        found = [self.get(i) for i in self.default_format_ids]
        return [f for f in found if f is not None]

    @property
    def default_audio(self) -> RemoteFormat | None:
        return next((f for f in self.default_formats if f.kind is FormatKind.AUDIO_ONLY), None)


def _audio_rank(fmt: RemoteFormat) -> tuple:
    return (fmt.language_preference, not fmt.is_drc, fmt.audio_bitrate or 0, fmt.asr or 0)


def _classify(raw: dict) -> tuple[FormatKind | None, bool]:
    vcodec, acodec = raw.get("vcodec"), raw.get("acodec")
    if vcodec == "none" and acodec == "none":
        return None, False  # storyboards, thumbnails
    if vcodec == "none":
        return FormatKind.AUDIO_ONLY, False
    if acodec == "none":
        return FormatKind.VIDEO_ONLY, False
    if vcodec is None and acodec is None:
        # yt-dlp uses None for "unknown" (e.g. a direct file link). Guess.
        if raw.get("height") or raw.get("width"):
            return FormatKind.COMBINED, True
        if str(raw.get("ext") or "").lower() in AUDIO_EXTENSIONS or raw.get("resolution") == "audio only":
            return FormatKind.AUDIO_ONLY, True
        return FormatKind.COMBINED, True
    return FormatKind.COMBINED, vcodec is None or acodec is None


def parse_format(raw: dict) -> RemoteFormat | None:
    if str(raw.get("ext") or "").lower() == "mhtml" or "storyboard" in str(raw.get("format_note") or "").lower():
        return None
    kind, guessed = _classify(raw)
    if kind is None:
        return None
    vcodec = raw.get("vcodec")
    acodec = raw.get("acodec")
    lang_pref = raw.get("language_preference")
    return RemoteFormat(
        format_id=str(raw.get("format_id") or ""),
        ext=str(raw.get("ext") or "").lower(),
        kind=kind,
        kind_is_guess=guessed,
        vcodec=None if vcodec in (None, "none") else str(vcodec),
        acodec=None if acodec in (None, "none") else str(acodec),
        width=_int(raw.get("width")),
        height=_int(raw.get("height")),
        fps=_num(raw.get("fps")),
        tbr=_num(raw.get("tbr")),
        vbr=_num(raw.get("vbr")),
        abr=_num(raw.get("abr")),
        asr=_int(raw.get("asr")),
        audio_channels=_int(raw.get("audio_channels")),
        filesize=_int(raw.get("filesize")),
        filesize_approx=_int(raw.get("filesize_approx")),
        protocol=str(raw.get("protocol") or ""),
        format_note=str(raw.get("format_note") or ""),
        language=raw.get("language") or None,
        language_preference=int(lang_pref) if isinstance(lang_pref, (int, float)) else -1,
        dynamic_range=raw.get("dynamic_range") or None,
    )


def parse_info_dict(info: dict, url: str = "") -> RemoteMedia:
    """Parse a single-video info dict (playlists must be rejected earlier)."""
    raw_formats = info.get("formats") or []
    if not raw_formats and info.get("url"):
        raw_formats = [info]  # extractor returned a single direct format
    formats: list[RemoteFormat] = []
    drm = 0
    seen: set[str] = set()
    for raw in raw_formats:
        if not isinstance(raw, dict):
            continue
        if raw.get("has_drm"):
            drm += 1
            continue
        parsed = parse_format(raw)
        if parsed is None or parsed.format_id in seen:
            continue
        seen.add(parsed.format_id)
        formats.append(parsed)
    default_ids = [i for i in str(info.get("format_id") or "").split("+") if i in seen]
    return RemoteMedia(
        url=url or info.get("webpage_url") or info.get("original_url") or "",
        title=str(info.get("title") or info.get("fulltitle") or info.get("id") or "download"),
        webpage_url=str(info.get("webpage_url") or ""),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        duration=_num(info.get("duration")),
        extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
        is_live=bool(info.get("is_live")),
        formats=formats,
        default_format_ids=default_ids,
        drm_formats_skipped=drm,
    )


def _same_video_variant(a: RemoteFormat, b: RemoteFormat) -> bool:
    return (
        a.kind is b.kind
        and a.height == b.height
        and round(a.fps or 0) == round(b.fps or 0)
        and (a.video_family is None or a.video_family == b.video_family)
    )


def recommended_formats(formats: list[RemoteFormat]) -> list[RemoteFormat]:
    """Hide obvious duplicates for a cleaner list; "show all" bypasses this.

    * streaming-manifest (HLS/DASH) variants when a direct download of the
      same kind/resolution/codec exists;
    * "DRC" (dynamic range compressed) audio when normal audio exists.
    Never returns an empty list if the input was not empty.
    """
    direct = [f for f in formats if not f.is_manifest]
    has_plain_audio = any(f.kind is FormatKind.AUDIO_ONLY and not f.is_drc for f in formats)
    result = []
    for fmt in formats:
        if fmt.is_drc and has_plain_audio:
            continue
        if fmt.is_manifest:
            if fmt.kind is FormatKind.AUDIO_ONLY:
                if any(d.kind is FormatKind.AUDIO_ONLY for d in direct):
                    continue
            elif any(_same_video_variant(fmt, d) for d in direct):
                continue
        result.append(fmt)
    return result or list(formats)
