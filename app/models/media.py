"""Local media information as reported by ffprobe."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.timefmt import format_duration
from app.utils.units import human_bitrate, human_size


def parse_fraction(value: object) -> float | None:
    """'30000/1001' -> 29.97; '0/0', 'N/A', junk -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("N/A", "0/0"):
        return None
    try:
        if "/" in text:
            num, den = (float(x) for x in text.split("/", 1))
            if den == 0:
                return None
            result = num / den
        else:
            result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) and result > 0 else None


def _int(value: object) -> int | None:
    try:
        number = int(float(value))  # ffprobe gives numbers as strings
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _rotation(stream: dict) -> int:
    """Clockwise rotation a player applies when displaying the stream."""
    degrees: float | None = None
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                # The display matrix angle is counter-clockwise.
                degrees = -float(side_data["rotation"])
            except (TypeError, ValueError):
                pass
            break
    if degrees is None:
        tag = (stream.get("tags") or {}).get("rotate")
        try:
            degrees = float(tag) if tag is not None else 0.0
        except (TypeError, ValueError):
            degrees = 0.0
    return int(round(degrees / 90.0)) * 90 % 360


@dataclass
class StreamInfo:
    index: int
    kind: str  # video / audio / subtitle / data / attachment
    codec: str = ""
    codec_long: str = ""
    profile: str = ""
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pix_fmt: str = ""
    bit_rate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str = ""
    sample_fmt: str = ""
    bits_per_sample: int | None = None
    language: str = ""
    title: str = ""
    duration: float | None = None
    rotation: int = 0
    is_cover_art: bool = False  # embedded album art shows up as a video stream
    is_default: bool = False

    @property
    def display_size(self) -> tuple[int, int] | None:
        if not self.width or not self.height:
            return None
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height


@dataclass
class MediaInfo:
    path: Path
    format_name: str = ""
    format_long: str = ""
    duration: float | None = None
    size: int | None = None
    bit_rate: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    streams: list[StreamInfo] = field(default_factory=list)

    @property
    def video_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "video" and not s.is_cover_art]

    @property
    def audio_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "audio"]

    @property
    def subtitle_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "subtitle"]

    @property
    def has_video(self) -> bool:
        return bool(self.video_streams)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_streams)

    @property
    def has_cover_art(self) -> bool:
        return any(s.is_cover_art for s in self.streams)

    @property
    def primary_video(self) -> StreamInfo | None:
        streams = self.video_streams
        return next((s for s in streams if s.is_default), streams[0] if streams else None)

    @property
    def primary_audio(self) -> StreamInfo | None:
        streams = self.audio_streams
        return next((s for s in streams if s.is_default), streams[0] if streams else None)

    @property
    def best_duration(self) -> float | None:
        if self.duration:
            return self.duration
        durations = [s.duration for s in self.streams if s.duration]
        return max(durations) if durations else None


def parse_ffprobe_json(data: dict, path: Path | str) -> MediaInfo:
    """Build :class:`MediaInfo` from ``ffprobe -print_format json -show_format -show_streams``."""
    fmt = data.get("format") or {}
    streams: list[StreamInfo] = []
    for raw in data.get("streams") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("codec_type") or "data"
        tags = {str(k).lower(): str(v) for k, v in (raw.get("tags") or {}).items()}
        disposition = raw.get("disposition") or {}
        fps = None
        if kind == "video":
            fps = parse_fraction(raw.get("avg_frame_rate")) or parse_fraction(raw.get("r_frame_rate"))
        bits = _int(raw.get("bits_per_raw_sample")) or _int(raw.get("bits_per_sample")) or None
        streams.append(
            StreamInfo(
                index=_int(raw.get("index")) or 0,
                kind=kind,
                codec=raw.get("codec_name") or "",
                codec_long=raw.get("codec_long_name") or "",
                profile=raw.get("profile") or "",
                width=_int(raw.get("width")) or None,
                height=_int(raw.get("height")) or None,
                fps=fps,
                pix_fmt=raw.get("pix_fmt") or "",
                bit_rate=_int(raw.get("bit_rate")) or _int(tags.get("bps")) or None,
                sample_rate=_int(raw.get("sample_rate")) or None,
                channels=_int(raw.get("channels")) or None,
                channel_layout=raw.get("channel_layout") or "",
                sample_fmt=raw.get("sample_fmt") or "",
                bits_per_sample=bits,
                language=tags.get("language", ""),
                title=tags.get("title", ""),
                duration=_positive_float(raw.get("duration")),
                rotation=_rotation(raw) if kind == "video" else 0,
                is_cover_art=bool(disposition.get("attached_pic")),
                is_default=bool(disposition.get("default")),
            )
        )
    return MediaInfo(
        path=Path(path),
        format_name=fmt.get("format_name") or "",
        format_long=fmt.get("format_long_name") or "",
        duration=_positive_float(fmt.get("duration")),
        size=_int(fmt.get("size")),
        bit_rate=_int(fmt.get("bit_rate")) or None,
        tags={str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
        streams=streams,
    )


def _channels_text(stream: StreamInfo) -> str:
    names = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
    if stream.channel_layout:
        return stream.channel_layout
    return names.get(stream.channels or 0, f"{stream.channels} channels" if stream.channels else "-")


def media_summary(info: MediaInfo) -> str:
    """Readable multi-line description for the "Information" panels."""
    lines = [
        f"File:       {info.path.name}",
        f"Folder:     {info.path.parent}",
        f"Format:     {info.format_long or info.format_name or '-'}",
        f"Duration:   {format_duration(info.best_duration)}",
        f"Size:       {human_size(info.size)}",
        f"Bitrate:    {human_bitrate(info.bit_rate / 1000 if info.bit_rate else None)}",
    ]
    for stream in info.streams:
        lang = f" [{stream.language}]" if stream.language and stream.language != "und" else ""
        title = f" \"{stream.title}\"" if stream.title else ""
        if stream.kind == "video" and stream.is_cover_art:
            size = f"{stream.width}x{stream.height}" if stream.width else "-"
            lines.append(f"Cover art:  {stream.codec} {size}")
        elif stream.kind == "video":
            size = f"{stream.width}x{stream.height}" if stream.width else "-"
            fps = f", {stream.fps:.3g} fps" if stream.fps else ""
            rot = f", rotated {stream.rotation} deg" if stream.rotation else ""
            rate = f", {human_bitrate(stream.bit_rate / 1000)}" if stream.bit_rate else ""
            lines.append(f"Video #{stream.index}:   {stream.codec.upper()} {size}{fps}{rate}{rot}{lang}{title}")
        elif stream.kind == "audio":
            sr = f", {stream.sample_rate / 1000:g} kHz" if stream.sample_rate else ""
            rate = f", {human_bitrate(stream.bit_rate / 1000)}" if stream.bit_rate else ""
            lines.append(f"Audio #{stream.index}:   {stream.codec.upper()}{sr}, {_channels_text(stream)}{rate}{lang}{title}")
        elif stream.kind == "subtitle":
            lines.append(f"Subtitle #{stream.index}: {stream.codec}{lang}{title}")
    if info.tags:
        shown = {k: v for k, v in info.tags.items() if k in ("title", "artist", "album", "date", "genre", "comment", "album_artist", "track")}
        for key, value in shown.items():
            lines.append(f"Tag {key}: {value}")
    return "\n".join(lines)
