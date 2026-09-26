"""Video operations built on FFmpeg.

Each operation lists one or more strategies (see ``Attempt``): lossless stream
copy first where that is safe, re-encoding as the fallback. Every result is
verified before it gets its final name (see ``MediaProcessor``).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from app.core.errors import InvalidInputError
from app.core.jobs import JobContext
from app.models.media import MediaInfo, StreamInfo, media_summary
from app.models.results import JobResult
from app.services import subtitles as subs
from app.services.ffmpeg.codecs import (
    AUDIO_FORMATS,
    audio_encoder_args,
    container_extra_args,
    video_audio_encoder_args,
    video_encoder_args,
)
from app.services.ffmpeg.processor import Attempt, Expectation, MediaProcessor
from app.services.subtitles import SubtitleStyle
from app.services.tools import MediaTools
from app.utils.timefmt import format_duration
from app.utils.units import human_size

log = logging.getLogger(__name__)

TRANSFORMS = {
    # key: (FFmpeg filter, status while running, label when done)
    "cw": ("transpose=1", "Rotating 90° clockwise", "Rotated 90° clockwise"),
    "ccw": ("transpose=2", "Rotating 90° counter-clockwise", "Rotated 90° counter-clockwise"),
    "180": ("hflip,vflip", "Rotating 180°", "Rotated 180°"),
    "hflip": ("hflip", "Flipping horizontally", "Flipped horizontally"),
    "vflip": ("vflip", "Flipping vertically", "Flipped vertically"),
}
COMPRESSION_LEVELS = {
    # key: (label, x264 CRF, audio kbps)
    "high": ("High quality (larger file)", 22, 160),
    "balanced": ("Balanced", 26, 128),
    "small": ("Smallest file (lower quality)", 30, 96),
}
MIN_SPEED, MAX_SPEED = 0.25, 4.0
MIN_VOLUME, MAX_VOLUME = 0.0, 5.0


def ts(seconds: float) -> str:
    """Seconds as an FFmpeg time argument."""
    return f"{max(0.0, seconds):.3f}"


def atempo_chain(factor: float) -> str:
    """FFmpeg's atempo accepts 0.5-2.0 per instance on older builds; chain
    several for larger changes (e.g. 4x -> atempo=2,atempo=2)."""
    if factor <= 0:
        raise ValueError("speed factor must be positive")
    parts = []
    remaining = factor
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6g}")
    return ",".join(parts)


def concat_list_text(paths: list[Path]) -> str:
    """Contents of an FFmpeg concat-demuxer list file. Single quotes inside
    paths are escaped the way the demuxer expects ('\\'')."""
    lines = ["ffconcat version 1.0"]
    for path in paths:
        escaped = str(Path(path).resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def _stream_signature(info: MediaInfo) -> tuple:
    video = info.primary_video
    audio = info.primary_audio
    return (
        len(info.video_streams),
        len(info.audio_streams),
        info.has_cover_art,
        video.codec if video else None,
        video.width if video else None,
        video.height if video else None,
        video.pix_fmt if video else None,
        round(video.fps or 0, 2) if video else None,
        audio.codec if audio else None,
        audio.sample_rate if audio else None,
        audio.channels if audio else None,
    )


def can_concat_copy(infos: list[MediaInfo]) -> bool:
    """Stream copy joining only works when every file has identical stream
    parameters; otherwise the result would be broken, so we re-encode."""
    return len({_stream_signature(i) for i in infos}) == 1


def _remux_is_sensible(ext: str, info: MediaInfo) -> bool:
    """Whether copying the streams into container ``ext`` is likely to give a
    file ordinary players accept. FFmpeg itself rejects many impossible
    combinations; this guards the ones it would accept but players would not."""
    video = info.primary_video
    audio = info.primary_audio
    vcodec = video.codec if video else None
    acodec = audio.codec if audio else None
    if ext == "webm":
        return vcodec in (None, "vp8", "vp9", "av1") and acodec in (None, "opus", "vorbis")
    if ext == "avi":
        return vcodec in (None, "mpeg4", "h264", "mjpeg") and acodec in (None, "mp3", "ac3", "pcm_s16le")
    if ext in ("mp4", "mov"):
        return vcodec in (None, "h264", "hevc", "av1", "mpeg4", "vp9") and acodec in (
            None, "aac", "mp3", "ac3", "eac3", "alac", "opus", "flac",
        )
    return True  # mkv holds anything


class VideoService:
    def __init__(self, tools: MediaTools):
        self.processor = MediaProcessor(tools)

    # -- helpers -----------------------------------------------------------
    def _probe_video(self, source: Path, ctx: JobContext) -> tuple[MediaInfo, StreamInfo]:
        ctx.set_status(f"Reading {source.name}...")
        ctx.set_progress(None)
        info = self.processor.probe(source)
        video = info.primary_video
        if video is None:
            raise InvalidInputError(f"'{source.name}' does not contain a video stream.")
        return info, video

    @staticmethod
    def _saved(output: Path, extra: str = "") -> JobResult:
        size = human_size(output.stat().st_size) if output.exists() else "-"
        return JobResult(f"Saved {output.name} ({size}){extra}", outputs=[output])

    def _filter_with_audio_fallback(
        self,
        source: Path,
        output: Path,
        info: MediaInfo,
        video_filter: str,
        ctx: JobContext,
        status: str,
        crf: int = 20,
    ) -> JobResult:
        """Re-encode video through ``video_filter``; copy the audio when the
        container allows it, else re-encode the audio too."""
        ext = output.suffix.lstrip(".").lower()
        video = info.primary_video
        assert video is not None
        base = ["-i", str(source), "-map", f"0:{video.index}", "-map", "0:a?", "-sn", "-dn", "-vf", video_filter,
                *video_encoder_args(ext, crf)]
        expect = Expectation(video=True, audio=info.has_audio, duration=info.best_duration)
        attempts = [
            Attempt("keeping audio as-is", lambda tmp: [*base, "-c:a", "copy", *container_extra_args(ext), str(tmp)], expect),
            Attempt("converting audio", lambda tmp: [*base, *video_audio_encoder_args(ext), *container_extra_args(ext), str(tmp)], expect),
        ]
        self.processor.render_first_working(output, attempts, ctx, status)
        return self._saved(output)

    # -- operations --------------------------------------------------------
    def info(self, source: Path, ctx: JobContext) -> JobResult:
        ctx.set_status(f"Reading {source.name}...")
        ctx.set_progress(None)
        info = self.processor.probe(source)
        return JobResult(f"Information for {source.name}", details=media_summary(info))

    def trim(
        self, source: Path, output: Path, start: float, end: float | None, precise: bool, ctx: JobContext
    ) -> JobResult:
        info, video = self._probe_video(source, ctx)
        duration = info.best_duration
        if end is None:
            end = duration
        if end is None:
            raise InvalidInputError("The length of this video is unknown; please enter an end time.")
        if duration is not None:
            if start >= duration:
                raise InvalidInputError(
                    f"The start time is after the end of the video (length {duration:.1f} s)."
                )
            end = min(end, duration)
        if end <= start:
            raise InvalidInputError("The end time must be after the start time.")
        length = end - start
        ext = output.suffix.lstrip(".").lower()
        extra = container_extra_args(ext)

        def copy_args(tmp: Path) -> list[str]:
            return ["-ss", ts(start), "-i", str(source), "-t", ts(length), "-map", "0:v?", "-map", "0:a?",
                    "-map", "0:s?", "-c", "copy", "-avoid_negative_ts", "make_zero", *extra, str(tmp)]

        def encode_args(tmp: Path) -> list[str]:
            return ["-ss", ts(start), "-i", str(source), "-t", ts(length), "-map", f"0:{video.index}",
                    "-map", "0:a?", "-sn", "-dn", *video_encoder_args(ext, crf=18),
                    *video_audio_encoder_args(ext, 192), *extra, str(tmp)]

        precise_expect = Expectation(video=True, audio=info.has_audio, duration=length)
        # Stream copy cuts at the nearest earlier keyframe, so the clip may be a little longer.
        fast_expect = Expectation(video=True, audio=info.has_audio, duration=length, tolerance=max(10.0, length * 0.15))
        if precise:
            attempts = [Attempt("precise", encode_args, precise_expect)]
        else:
            attempts = [Attempt("fast copy", copy_args, fast_expect), Attempt("re-encoding", encode_args, precise_expect)]
        _, used = self.processor.render_first_working(output, attempts, ctx, "Trimming video", progress_duration=length)
        note = " - fast mode, cut at the nearest keyframe" if used == "fast copy" else ""
        return self._saved(output, note)

    def merge(self, sources: list[Path], output: Path, ctx: JobContext) -> JobResult:
        if len(sources) < 2:
            raise InvalidInputError("Please add at least two videos to join.")
        infos: list[MediaInfo] = []
        for number, source in enumerate(sources, start=1):
            ctx.set_status(f"Checking video {number} of {len(sources)}...")
            ctx.step(number - 1, len(sources))
            info = self.processor.probe(source)
            if not info.has_video:
                raise InvalidInputError(f"'{source.name}' does not contain a video stream.")
            infos.append(info)
        total = sum(i.best_duration or 0 for i in infos) or None
        any_audio = any(i.has_audio for i in infos)
        ext = output.suffix.lstrip(".").lower()
        expect = Expectation(video=True, audio=any_audio, duration=total,
                             tolerance=max(2.0, (total or 0) * 0.02 + 0.5 * len(infos)))
        workdir = Path(tempfile.mkdtemp(prefix="mt-merge-"))
        try:
            list_file = workdir / "list.txt"
            list_file.write_text(concat_list_text(sources), encoding="utf-8")

            def copy_args(tmp: Path) -> list[str]:
                return ["-f", "concat", "-safe", "0", "-i", str(list_file), "-map", "0:v", "-map", "0:a?",
                        "-c", "copy", *container_extra_args(ext), str(tmp)]

            attempts = []
            if can_concat_copy(infos):
                attempts.append(Attempt("fast join", copy_args, expect))
            attempts.append(Attempt("re-encoding", lambda tmp: self._concat_filter_args(sources, infos, ext, tmp), expect))
            _, used = self.processor.render_first_working(output, attempts, ctx, f"Joining {len(sources)} videos",
                                                          progress_duration=total)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        note = " - joined without re-encoding" if used == "fast join" else ""
        return self._saved(output, note)

    @staticmethod
    def _concat_filter_args(sources: list[Path], infos: list[MediaInfo], ext: str, tmp: Path) -> list[str]:
        first = infos[0].primary_video
        assert first is not None
        width, height = first.display_size or (1280, 720)
        width, height = width - width % 2, height - height % 2
        fps = round(first.fps, 3) if first.fps and first.fps < 240 else 30
        any_audio = any(i.has_audio for i in infos)
        args: list[str] = []
        parts: list[str] = []
        labels: list[str] = []
        for number, (source, info) in enumerate(zip(sources, infos, strict=True)):
            args += ["-i", str(source)]
            video = info.primary_video
            assert video is not None
            parts.append(
                f"[{number}:{video.index}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p[v{number}]"
            )
            labels.append(f"[v{number}]")
            if any_audio:
                audio = info.primary_audio
                if audio is not None:
                    parts.append(
                        f"[{number}:{audio.index}]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a{number}]"
                    )
                else:
                    # Silence for clips without sound; concat pads it to the clip's length.
                    length = info.best_duration or 0.1
                    parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={length:.3f}[a{number}]")
                labels.append(f"[a{number}]")
        audio_count = 1 if any_audio else 0
        parts.append(f"{''.join(labels)}concat=n={len(sources)}:v=1:a={audio_count}[outv]" + ("[outa]" if any_audio else ""))
        args += ["-filter_complex", ";".join(parts), "-map", "[outv]"]
        if any_audio:
            args += ["-map", "[outa]", *video_audio_encoder_args(ext, 192)]
        args += [*video_encoder_args(ext, crf=20), *container_extra_args(ext), str(tmp)]
        return args

    def extract_audio(
        self,
        source: Path,
        output: Path,
        target: str,
        bitrate: int | None,
        ctx: JobContext,
        stream_index: int | None = None,
    ) -> JobResult:
        """``target`` is 'copy' (keep the original audio, no quality loss) or a
        key of ``AUDIO_FORMATS``."""
        ctx.set_status(f"Reading {source.name}...")
        info = self.processor.probe(source)
        if not info.has_audio:
            raise InvalidInputError(f"'{source.name}' does not contain any audio.")
        stream = next((s for s in info.audio_streams if s.index == stream_index), None) or info.primary_audio
        assert stream is not None
        ext = output.suffix.lstrip(".").lower()
        duration = stream.duration or info.best_duration
        expect = Expectation(video=False, audio=True, duration=duration)
        base = ["-i", str(source), "-map", f"0:{stream.index}", "-vn", "-sn", "-dn"]
        if target == "copy":
            attempts = [
                Attempt("copying", lambda tmp: [*base, "-c:a", "copy", *container_extra_args(ext), str(tmp)], expect),
                Attempt("converting", lambda tmp: [*base, *container_extra_args(ext), str(tmp)], expect),
            ]
        else:
            fmt = AUDIO_FORMATS[target]
            attempts = [Attempt("converting", lambda tmp: [*base, *audio_encoder_args(fmt, bitrate), str(tmp)], expect)]
        self.processor.render_first_working(output, attempts, ctx, "Extracting audio", progress_duration=duration)
        return self._saved(output)

    def remove_audio(self, source: Path, output: Path, ctx: JobContext) -> JobResult:
        info, video = self._probe_video(source, ctx)
        ext = output.suffix.lstrip(".").lower()
        extra = container_extra_args(ext)
        expect = Expectation(video=True, audio=False, duration=info.best_duration)
        attempts = [
            Attempt("copying", lambda tmp: ["-i", str(source), "-map", "0", "-map", "-0:a", "-c", "copy", *extra, str(tmp)], expect),
            Attempt("copying video only", lambda tmp: ["-i", str(source), "-map", f"0:{video.index}", "-c", "copy", *extra, str(tmp)], expect),
            Attempt("re-encoding", lambda tmp: ["-i", str(source), "-map", f"0:{video.index}", "-an", "-sn", "-dn",
                                                *video_encoder_args(ext), *extra, str(tmp)], expect),
        ]
        self.processor.render_first_working(output, attempts, ctx, "Removing audio", progress_duration=info.best_duration)
        return self._saved(output)

    def extract_video(self, source: Path, output: Path, ctx: JobContext) -> JobResult:
        info, video = self._probe_video(source, ctx)
        ext = output.suffix.lstrip(".").lower()
        extra = container_extra_args(ext)
        expect = Expectation(video=True, audio=False, duration=video.duration or info.best_duration)
        attempts = [
            Attempt("copying", lambda tmp: ["-i", str(source), "-map", f"0:{video.index}", "-an", "-sn", "-dn",
                                            "-c:v", "copy", *extra, str(tmp)], expect),
            Attempt("re-encoding", lambda tmp: ["-i", str(source), "-map", f"0:{video.index}", "-an", "-sn", "-dn",
                                                *video_encoder_args(ext), *extra, str(tmp)], expect),
        ]
        self.processor.render_first_working(output, attempts, ctx, "Extracting the video stream",
                                            progress_duration=info.best_duration)
        return self._saved(output)

    def convert(self, source: Path, output: Path, force_reencode: bool, ctx: JobContext, crf: int = 20) -> JobResult:
        info, video = self._probe_video(source, ctx)
        ext = output.suffix.lstrip(".").lower()
        extra = container_extra_args(ext)
        expect = Expectation(video=True, audio=info.has_audio, duration=info.best_duration)
        attempts = []
        if not force_reencode and _remux_is_sensible(ext, info):
            attempts.append(Attempt("changing container only", lambda tmp: [
                "-i", str(source), "-map", f"0:{video.index}", "-map", "0:a?", "-c", "copy", *extra, str(tmp)], expect))
        attempts.append(Attempt("re-encoding", lambda tmp: [
            "-i", str(source), "-map", f"0:{video.index}", "-map", "0:a?", "-sn", "-dn",
            *video_encoder_args(ext, crf), *video_audio_encoder_args(ext, 192), *extra, str(tmp)], expect))
        _, used = self.processor.render_first_working(output, attempts, ctx, f"Converting to {ext.upper()}",
                                                      progress_duration=info.best_duration)
        note = " - no quality loss (streams copied)" if used == "changing container only" else ""
        return self._saved(output, note)

    def resize(
        self, source: Path, output: Path, width: int | None, height: int | None, keep_aspect: bool, ctx: JobContext
    ) -> JobResult:
        if not width and not height:
            raise InvalidInputError("Please enter a width or a height.")
        for value in (width, height):
            if value is not None and not (16 <= value <= 8192):
                raise InvalidInputError("Width and height must be between 16 and 8192 pixels.")
        info, _ = self._probe_video(source, ctx)
        if width and height and keep_aspect:
            flt = f"scale={width}:{height}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
        elif width and height:
            flt = f"scale={width - width % 2}:{height - height % 2},setsar=1"
        elif height:
            flt = f"scale=-2:{height - height % 2}"
        else:
            assert width is not None
            flt = f"scale={width - width % 2}:-2"
        return self._filter_with_audio_fallback(source, output, info, flt, ctx, "Resizing video")

    def transform(self, source: Path, output: Path, operation: str, ctx: JobContext) -> JobResult:
        if operation not in TRANSFORMS:
            raise InvalidInputError(f"Unknown rotation option: {operation}")
        info, _ = self._probe_video(source, ctx)
        flt, status, label = TRANSFORMS[operation]
        result = self._filter_with_audio_fallback(source, output, info, flt, ctx, status)
        result.message = f"{label}: {result.message}"
        return result

    def change_speed(self, source: Path, output: Path, factor: float, ctx: JobContext) -> JobResult:
        if not (MIN_SPEED <= factor <= MAX_SPEED):
            raise InvalidInputError(f"Speed must be between {MIN_SPEED}x and {MAX_SPEED}x.")
        info, video = self._probe_video(source, ctx)
        ext = output.suffix.lstrip(".").lower()
        audio = info.primary_audio
        new_duration = (info.best_duration / factor) if info.best_duration else None
        graph = f"[0:{video.index}]setpts=PTS/{factor:.6g}[v]"
        maps = ["-map", "[v]"]
        audio_args: list[str] = []
        if audio is not None:
            graph += f";[0:{audio.index}]{atempo_chain(factor)}[a]"
            maps += ["-map", "[a]"]
            audio_args = video_audio_encoder_args(ext, 192)
        expect = Expectation(video=True, audio=audio is not None, duration=new_duration)
        attempt = Attempt("re-encoding", lambda tmp: ["-i", str(source), "-filter_complex", graph, *maps,
                                                      *video_encoder_args(ext), *audio_args,
                                                      *container_extra_args(ext), str(tmp)], expect)
        self.processor.render_first_working(output, [attempt], ctx, f"Changing speed to {factor:g}x",
                                            progress_duration=new_duration)
        return self._saved(output)

    def change_volume(self, source: Path, output: Path, factor: float, ctx: JobContext) -> JobResult:
        if not (MIN_VOLUME <= factor <= MAX_VOLUME):
            raise InvalidInputError("Volume must be between 0% and 500%.")
        info, video = self._probe_video(source, ctx)
        if not info.has_audio:
            raise InvalidInputError(f"'{source.name}' has no audio, so its volume cannot be changed.")
        ext = output.suffix.lstrip(".").lower()
        expect = Expectation(video=True, audio=True, duration=info.best_duration)
        attempt = Attempt("re-encoding audio", lambda tmp: [
            "-i", str(source), "-map", f"0:{video.index}", "-map", "0:a", "-c:v", "copy", "-af", f"volume={factor:.4g}",
            *video_audio_encoder_args(ext, 192), *container_extra_args(ext), str(tmp)], expect)
        fallback = Attempt("re-encoding everything", lambda tmp: [
            "-i", str(source), "-map", f"0:{video.index}", "-map", "0:a", "-af", f"volume={factor:.4g}",
            *video_encoder_args(ext), *video_audio_encoder_args(ext, 192), *container_extra_args(ext), str(tmp)], expect)
        self.processor.render_first_working(output, [attempt, fallback], ctx, f"Changing volume to {factor:.0%}",
                                            progress_duration=info.best_duration)
        return self._saved(output)

    def compress(self, source: Path, output: Path, level: str, max_height: int | None, ctx: JobContext) -> JobResult:
        if level not in COMPRESSION_LEVELS:
            raise InvalidInputError(f"Unknown compression level: {level}")
        _, crf, audio_kbps = COMPRESSION_LEVELS[level]
        info, video = self._probe_video(source, ctx)
        ext = output.suffix.lstrip(".").lower()
        vf = ["-vf", f"scale=-2:min(ih\\,{max_height - max_height % 2})"] if max_height else []
        expect = Expectation(video=True, audio=info.has_audio, duration=info.best_duration)
        attempt = Attempt("compressing", lambda tmp: [
            "-i", str(source), "-map", f"0:{video.index}", "-map", "0:a?", "-sn", "-dn", *vf,
            *video_encoder_args(ext, crf), *video_audio_encoder_args(ext, audio_kbps),
            *container_extra_args(ext), str(tmp)], expect)
        self.processor.render_first_working(output, [attempt], ctx, "Compressing video",
                                            progress_duration=info.best_duration)
        before = source.stat().st_size
        after = output.stat().st_size
        result = self._saved(output)
        if before > 0:
            change = (1 - after / before) * 100
            if change > 0:
                result.message += f" - {change:.0f}% smaller"
            else:
                result.warnings.append(
                    "The compressed file is not smaller than the original. The original was already "
                    "efficiently compressed; try 'Smallest file' or a lower resolution."
                )
        return result

    def burn_subtitles(
        self,
        source: Path,
        subtitle: Path,
        output: Path,
        style: SubtitleStyle,
        ctx: JobContext,
        encoding: str = "auto",
        offset: float = 0.0,
    ) -> JobResult:
        """Draw the subtitles of ``subtitle`` (SRT/ASS/SSA/VTT) permanently into
        the picture. ``offset`` shifts them in seconds (positive = later)."""
        if abs(offset) > subs.MAX_OFFSET_SECONDS:
            raise InvalidInputError("The timing adjustment must be less than one hour.")
        style.validate()
        info, video = self._probe_video(source, ctx)
        duration = info.best_duration
        ext = output.suffix.lstrip(".").lower()
        work = Path(tempfile.mkdtemp(prefix="mt-subs-"))
        try:
            ctx.set_status(f"Reading {subtitle.name}...")
            copy, used_encoding = subs.prepare_subtitles(subtitle, work, encoding)
            times = [t + offset for t in subs.cue_start_times(self.processor.tools.ffprobe, copy)]
            if not times:
                raise InvalidInputError(
                    f"No subtitles were found in '{subtitle.name}'. Check that it is a valid "
                    f"{subs.subtitle_ext(subtitle).upper()} file, or choose its text encoding by hand.")
            if duration and times[0] >= duration:
                raise InvalidInputError(
                    f"All subtitles start after the end of the video (the first one at {format_duration(times[0])}, "
                    f"the video is {format_duration(duration)} long). Check that the subtitle file belongs to this "
                    "video, or adjust the timing.")
            shown = [t for t in times if t >= 0 and (not duration or t < duration)]
            if not shown:
                raise InvalidInputError("With this timing adjustment every subtitle falls before the start of the "
                                        "video. Use a smaller adjustment.")
            options = f"filename={copy.name}:charenc=UTF-8"
            if not (style.keep_file_style and subs.subtitle_ext(subtitle) in subs.STYLED_EXTENSIONS):
                options += f":force_style='{style.force_style()}'"
            flt = f"subtitles={options}"
            if offset:
                # The subtitles filter uses the frame time; shift it for the filter only, then back.
                flt = f"setpts=PTS-({offset:.3f})/TB,{flt},setpts=PTS+({offset:.3f})/TB"
            base = ["-i", str(source), "-map", f"0:{video.index}", "-map", "0:a?", "-sn", "-dn", "-vf", flt,
                    *video_encoder_args(ext, 20)]
            expect = Expectation(video=True, audio=info.has_audio, duration=duration)
            attempts = [
                Attempt("keeping audio as-is", lambda tmp: [*base, "-c:a", "copy", *container_extra_args(ext),
                                                            str(tmp)], expect),
                Attempt("converting audio", lambda tmp: [*base, *video_audio_encoder_args(ext),
                                                         *container_extra_args(ext), str(tmp)], expect),
            ]
            self.processor.render_first_working(output, attempts, ctx, "Burning in subtitles",
                                                progress_duration=duration, cwd=work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        note = f" - {len(shown)} subtitle(s) burned in"
        if encoding == "auto" and used_encoding not in ("utf-8", "utf-16", "ascii"):
            # Tell the user which legacy encoding was assumed, so wrong letters are easy to fix.
            note += f" (text read as {subs.encoding_label(used_encoding)})"
        result = self._saved(output, note)
        skipped = len(times) - len(shown)
        if skipped:
            result.warnings.append(f"{skipped} subtitle(s) fall outside the video's length and were not shown. "
                                   "If that is unexpected, check the timing adjustment or the subtitle file.")
        return result
