"""Turning the user's format choice into an exact yt-dlp request.

Pure logic (no network), so it is fully unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.remote import FormatKind, RemoteFormat, RemoteMedia, codec_family

# Codecs FFmpeg can put into each container when merging video + audio.
MP4_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4"}
MP4_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "flac", "alac"}
# Stricter sets used for "Auto": combinations that play in nearly every player.
MP4_SAFE_VIDEO = {"h264", "hevc", "av1"}
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3"}
WEBM_VIDEO = {"vp8", "vp9", "av1"}
WEBM_AUDIO = {"opus", "vorbis"}

AUDIO_TARGETS = {
    "original": "Keep original (no conversion)",
    "mp3": "MP3",
    "m4a": "M4A (AAC)",
    "wav": "WAV (uncompressed)",
    "flac": "FLAC (lossless)",
    "opus": "OPUS",
}
BITRATE_TARGETS = {"mp3", "m4a", "opus"}
# yt-dlp keeps these extensions as-is for "best" audio extraction.
_COMMON_AUDIO_EXTS = {"aiff", "alac", "flac", "m4a", "mka", "mp3", "ogg", "opus", "wav", "wma"}
_AUDIO_COPY_EXT = {"aac": "m4a", "mp3": "mp3", "opus": "opus", "vorbis": "ogg", "flac": "flac", "alac": "m4a"}


@dataclass
class DownloadPlan:
    format_selector: str  # value for yt-dlp's "format" option
    format_ids: list[str]  # the individual formats that will be downloaded
    merge_container: str | None = None  # container when video + audio are merged
    audio_target: str | None = None  # yt-dlp FFmpegExtractAudio codec ('best', 'mp3', ...)
    audio_bitrate: int | None = None
    final_ext: str | None = None  # expected extension; None if only known afterwards
    summary: str = ""
    notes: list[str] = field(default_factory=list)


def choose_container(video: RemoteFormat, audio: RemoteFormat, preference: str) -> tuple[str, str]:
    """Container for merging ``video`` + ``audio``; returns (ext, note)."""
    v, a = codec_family(video.vcodec), codec_family(audio.acodec)
    if v is None or a is None:
        exts = {video.ext, audio.ext}
        if preference in ("mp4", "auto") and exts <= {"mp4", "m4a"}:
            return "mp4", ""
        if preference in ("webm", "auto") and exts <= {"webm"}:
            return "webm", ""
        if preference == "mkv" or preference == "auto":
            return "mkv", ""
        return "mkv", f"The stream codecs are unknown, so MKV is used instead of {preference.upper()}."
    if preference == "mkv":
        return "mkv", ""
    if preference == "mp4":
        if v in MP4_VIDEO and a in MP4_AUDIO:
            return "mp4", ""
        return "mkv", "MP4 cannot hold these codecs, so MKV is used instead."
    if preference == "webm":
        if v in WEBM_VIDEO and a in WEBM_AUDIO:
            return "webm", ""
        return "mkv", "WEBM cannot hold these codecs, so MKV is used instead."
    if v in MP4_SAFE_VIDEO and a in MP4_SAFE_AUDIO:
        return "mp4", ""
    if v in WEBM_VIDEO and a in WEBM_AUDIO:
        return "webm", ""
    return "mkv", ""


def pick_audio_for_video(media: RemoteMedia, video: RemoteFormat, preference: str) -> RemoteFormat | None:
    """Best audio-only format to pair with a video-only format.

    Keeps the language yt-dlp itself preferred (original track rather than a
    dub), then favours codecs that fit the target container, then bitrate.
    """
    candidates = [f for f in media.formats if f.kind is FormatKind.AUDIO_ONLY]
    if not candidates:
        return None
    reference = media.default_audio
    language = reference.language if reference else None
    vfam = codec_family(video.vcodec)
    want_mp4 = preference == "mp4" or (preference == "auto" and vfam in MP4_SAFE_VIDEO)
    want_webm = preference == "webm" or (preference == "auto" and vfam in WEBM_VIDEO and vfam not in MP4_SAFE_VIDEO)

    def rank(fmt: RemoteFormat) -> tuple:
        afam = codec_family(fmt.acodec)
        fits = (want_mp4 and afam in MP4_SAFE_AUDIO) or (want_webm and afam in WEBM_AUDIO)
        same_language = language is None or fmt.language == language
        return (same_language, fmt.language_preference, not fmt.is_drc, not fmt.is_manifest, fits,
                fmt.audio_bitrate or 0, fmt.asr or 0)

    return max(candidates, key=rank)


def _describe_pair(video: RemoteFormat, audio: RemoteFormat, container: str) -> str:
    return f"{video.short_label()} + {audio.short_label()} audio, saved as {container.upper()}"


def plan_video_download(
    media: RemoteMedia, selected: RemoteFormat | None, container_pref: str, add_audio: bool
) -> DownloadPlan:
    """``selected=None`` means "best available" (yt-dlp's own choice)."""
    if selected is None:
        chosen = media.default_formats
        videos = [f for f in chosen if f.kind is FormatKind.VIDEO_ONLY]
        audios = [f for f in chosen if f.kind is FormatKind.AUDIO_ONLY]
        if len(chosen) == 2 and videos and audios:
            container, note = choose_container(videos[0], audios[0], container_pref)
            return DownloadPlan(
                f"{videos[0].format_id}+{audios[0].format_id}",
                [videos[0].format_id, audios[0].format_id],
                merge_container=container,
                final_ext=container,
                summary="Best available: " + _describe_pair(videos[0], audios[0], container),
                notes=[note] if note else [],
            )
        if len(chosen) == 1:
            return _single(chosen[0], "Best available: ")
        preference = {"auto": "mp4/webm/mkv", "mp4": "mp4", "mkv": "mkv", "webm": "webm"}.get(container_pref, "mkv")
        return DownloadPlan("bv*+ba/b", [], merge_container=preference, final_ext=None,
                            summary="Best available (chosen by yt-dlp)")

    if selected.kind is FormatKind.VIDEO_ONLY:
        if add_audio:
            audio = pick_audio_for_video(media, selected, container_pref)
            if audio is not None:
                container, note = choose_container(selected, audio, container_pref)
                return DownloadPlan(
                    f"{selected.format_id}+{audio.format_id}",
                    [selected.format_id, audio.format_id],
                    merge_container=container,
                    final_ext=container,
                    summary=_describe_pair(selected, audio, container),
                    notes=[note] if note else [],
                )
            plan = _single(selected)
            plan.notes.append("This site offers no separate audio, so the video will have no sound.")
            return plan
        plan = _single(selected)
        plan.notes.append("Video only: the file will have no sound.")
        return plan
    return _single(selected)


def _single(fmt: RemoteFormat, prefix: str = "") -> DownloadPlan:
    what = {
        FormatKind.COMBINED: "video with sound",
        FormatKind.VIDEO_ONLY: "video only",
        FormatKind.AUDIO_ONLY: "audio only",
    }[fmt.kind]
    return DownloadPlan(
        fmt.format_id,
        [fmt.format_id],
        final_ext=fmt.ext or None,
        summary=f"{prefix}{fmt.short_label()} ({what}), saved as {(fmt.ext or '?').upper()}",
    )


def extracted_audio_ext(fmt: RemoteFormat) -> str | None:
    """Extension yt-dlp produces for "keep original" audio extraction."""
    if fmt.ext in _COMMON_AUDIO_EXTS:
        return fmt.ext
    family = codec_family(fmt.acodec)
    return _AUDIO_COPY_EXT.get(family or "")


def plan_audio_download(
    media: RemoteMedia, selected: RemoteFormat | None, target: str, bitrate: int | None
) -> DownloadPlan:
    if target not in AUDIO_TARGETS:
        raise ValueError(f"unknown audio target {target}")
    if selected is None:
        selected = media.default_audio or next(iter(media.audio_formats), None)
    if selected is None:
        # No audio-only stream: take the best combined format and extract its audio.
        selector, ids, source_label = "ba/b", [], "best available audio"
        source_ext = None
    else:
        selector, ids, source_label = selected.format_id, [selected.format_id], selected.short_label()
        source_ext = extracted_audio_ext(selected)
    if target == "original":
        return DownloadPlan(selector, ids, audio_target="best", final_ext=source_ext,
                            summary=f"{source_label}, original quality")
    use_bitrate = bitrate if target in BITRATE_TARGETS else None
    rate = f" at {use_bitrate} kbps" if use_bitrate else ""
    plan = DownloadPlan(selector, ids, audio_target=target, audio_bitrate=use_bitrate, final_ext=target,
                        summary=f"{source_label}, converted to {AUDIO_TARGETS[target]}{rate}")
    if selected is not None and use_bitrate and selected.audio_bitrate and use_bitrate > selected.audio_bitrate * 1.15:
        plan.notes.append(
            f"The source is about {selected.audio_bitrate:.0f} kbps. Converting to {use_bitrate} kbps "
            "makes the file larger but cannot improve the sound."
        )
    return plan
