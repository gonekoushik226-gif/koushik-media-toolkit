"""Audio operations built on FFmpeg."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from app.core.errors import InvalidInputError
from app.core.jobs import JobContext
from app.models.media import MediaInfo, StreamInfo, media_summary
from app.models.results import JobResult
from app.services.ffmpeg.codecs import (
    AUDIO_FORMATS,
    AudioFormat,
    audio_encoder_args,
    audio_format_for_ext,
    container_extra_args,
    is_high_bit_depth,
    nearest_bitrate,
)
from app.services.ffmpeg.processor import Attempt, Expectation, MediaProcessor
from app.services.tools import MediaTools
from app.services.video import concat_list_text, ts
from app.utils.units import human_size

log = logging.getLogger(__name__)

EDITABLE_TAGS = (
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("album_artist", "Album artist"),
    ("date", "Year / date"),
    ("genre", "Genre"),
    ("track", "Track number"),
    ("comment", "Comment"),
)


def fade_filter(duration: float | None, fade_in: float, fade_out: float) -> str:
    if fade_in < 0 or fade_out < 0:
        raise InvalidInputError("Fade lengths cannot be negative.")
    if fade_in == 0 and fade_out == 0:
        raise InvalidInputError("Please enter a fade-in and/or fade-out length.")
    parts = []
    if fade_in > 0:
        parts.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        if not duration:
            raise InvalidInputError("The length of this file is unknown, so a fade-out cannot be placed.")
        if fade_in + fade_out > duration:
            raise InvalidInputError(
                f"The fades ({fade_in + fade_out:g} s) are longer than the file ({duration:.1f} s)."
            )
        parts.append(f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}")
    return ",".join(parts)


class AudioService:
    def __init__(self, tools: MediaTools):
        self.processor = MediaProcessor(tools)

    # -- helpers -----------------------------------------------------------
    def _probe_audio(self, source: Path, ctx: JobContext) -> tuple[MediaInfo, StreamInfo]:
        ctx.set_status(f"Reading {source.name}...")
        ctx.set_progress(None)
        info = self.processor.probe(source)
        stream = info.primary_audio
        if stream is None:
            raise InvalidInputError(f"'{source.name}' does not contain any audio.")
        return info, stream

    @staticmethod
    def _format_for_output(output: Path) -> AudioFormat:
        fmt = audio_format_for_ext(output.suffix)
        if fmt is None:
            raise InvalidInputError(f"Saving as '{output.suffix}' is not supported. Choose MP3, M4A, WAV, FLAC, OPUS or OGG.")
        return fmt

    @staticmethod
    def _saved(output: Path) -> JobResult:
        size = human_size(output.stat().st_size) if output.exists() else "-"
        return JobResult(f"Saved {output.name} ({size})", outputs=[output])

    def _encode_attempts(
        self,
        source: Path,
        info: MediaInfo,
        stream: StreamInfo,
        fmt: AudioFormat,
        bitrate: int | None,
        audio_filter: str | None,
        expect: Expectation,
        pre_input: list[str] | None = None,
        post_input: list[str] | None = None,
    ) -> list[Attempt]:
        """Encode ``stream`` to ``fmt``; first try keeping embedded cover art
        (MP3/M4A/FLAC), then without it."""
        pre = pre_input or []
        post = post_input or []
        encoder = audio_encoder_args(fmt, bitrate,
                                     is_high_bit_depth(stream.sample_fmt, stream.bits_per_sample, stream.codec))
        afilter = ["-af", audio_filter] if audio_filter else []
        base = [*pre, "-i", str(source), *post, "-map", f"0:{stream.index}"]
        attempts = []
        cover = next((s for s in info.streams if s.is_cover_art), None)
        if cover is not None and fmt.keeps_cover_art:
            attempts.append(Attempt("with cover art", lambda tmp: [
                *base, "-map", f"0:{cover.index}", "-c:v", "copy", "-disposition:v:0", "attached_pic",
                *afilter, *encoder, *container_extra_args(fmt.ext), str(tmp)], expect))
        attempts.append(Attempt("encoding", lambda tmp: [
            *base, "-vn", "-sn", "-dn", *afilter, *encoder, *container_extra_args(fmt.ext), str(tmp)], expect))
        return attempts

    # -- operations --------------------------------------------------------
    def info(self, source: Path, ctx: JobContext) -> JobResult:
        ctx.set_status(f"Reading {source.name}...")
        ctx.set_progress(None)
        info = self.processor.probe(source)
        return JobResult(f"Information for {source.name}", details=media_summary(info))

    def trim(self, source: Path, output: Path, start: float, end: float | None, ctx: JobContext) -> JobResult:
        info, stream = self._probe_audio(source, ctx)
        duration = info.best_duration
        end = duration if end is None else (min(end, duration) if duration else end)
        if end is None:
            raise InvalidInputError("The length of this file is unknown; please enter an end time.")
        if duration is not None and start >= duration:
            raise InvalidInputError(f"The start time is after the end of the file (length {duration:.1f} s).")
        if end <= start:
            raise InvalidInputError("The end time must be after the start time.")
        length = end - start
        fmt = self._format_for_output(output)
        expect = Expectation(audio=True, duration=length, tolerance=max(1.0, length * 0.02))
        seek = ["-ss", ts(start)]
        limit = ["-t", ts(length)]
        attempts = []
        if output.suffix.lower() == source.suffix.lower():
            attempts.append(Attempt("fast copy", lambda tmp: [
                *seek, "-i", str(source), *limit, "-map", f"0:{stream.index}", "-vn", "-c:a", "copy",
                *container_extra_args(fmt.ext), str(tmp)], expect))
        bitrate = nearest_bitrate(stream.bit_rate or info.bit_rate)
        attempts += self._encode_attempts(source, info, stream, fmt, bitrate, None, expect, seek, limit)
        self.processor.render_first_working(output, attempts, ctx, "Trimming audio", progress_duration=length)
        return self._saved(output)

    def merge(self, sources: list[Path], output: Path, bitrate: int | None, ctx: JobContext) -> JobResult:
        if len(sources) < 2:
            raise InvalidInputError("Please add at least two audio files to join.")
        infos: list[MediaInfo] = []
        for number, source in enumerate(sources, start=1):
            ctx.set_status(f"Checking file {number} of {len(sources)}...")
            ctx.step(number - 1, len(sources))
            info = self.processor.probe(source)
            if not info.has_audio:
                raise InvalidInputError(f"'{source.name}' does not contain any audio.")
            infos.append(info)
        fmt = self._format_for_output(output)
        total = sum(i.best_duration or 0 for i in infos) or None
        expect = Expectation(audio=True, duration=total, tolerance=max(2.0, (total or 0) * 0.02))
        first = infos[0].primary_audio
        assert first is not None
        sample_rate = first.sample_rate or 44100
        layout = "mono" if (first.channels or 2) == 1 else "stereo"
        signatures = {(i.primary_audio.codec, i.primary_audio.sample_rate, i.primary_audio.channels) for i in infos}  # type: ignore[union-attr]
        same_format = len(signatures) == 1 and all(len(i.audio_streams) == 1 for i in infos)
        workdir = Path(tempfile.mkdtemp(prefix="mt-merge-"))
        try:
            list_file = workdir / "list.txt"
            list_file.write_text(concat_list_text(sources), encoding="utf-8")
            attempts = []
            if same_format and all(s.suffix.lower() == output.suffix.lower() for s in sources):
                attempts.append(Attempt("fast join", lambda tmp: [
                    "-f", "concat", "-safe", "0", "-i", str(list_file), "-map", "0:a", "-c", "copy",
                    *container_extra_args(fmt.ext), str(tmp)], expect))

            def encode_args(tmp: Path) -> list[str]:
                args: list[str] = []
                parts = []
                for number, (source, info) in enumerate(zip(sources, infos, strict=True)):
                    args += ["-i", str(source)]
                    stream = info.primary_audio
                    assert stream is not None
                    parts.append(f"[{number}:{stream.index}]aresample={sample_rate},"
                                 f"aformat=sample_rates={sample_rate}:channel_layouts={layout}[a{number}]")
                labels = "".join(f"[a{n}]" for n in range(len(sources)))
                parts.append(f"{labels}concat=n={len(sources)}:v=0:a=1[out]")
                return [*args, "-filter_complex", ";".join(parts), "-map", "[out]",
                        *audio_encoder_args(fmt, bitrate), *container_extra_args(fmt.ext), str(tmp)]

            attempts.append(Attempt("re-encoding", encode_args, expect))
            self.processor.render_first_working(output, attempts, ctx, f"Joining {len(sources)} files",
                                                progress_duration=total)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self._saved(output)

    def convert(self, source: Path, output: Path, format_key: str, bitrate: int | None, ctx: JobContext) -> JobResult:
        """Convert to another format and/or bitrate (also used for "Extract
        audio from video")."""
        fmt = AUDIO_FORMATS.get(format_key)
        if fmt is None:
            raise InvalidInputError(f"Unknown audio format: {format_key}")
        info, stream = self._probe_audio(source, ctx)
        # Cover art is not counted as video, so audio files must never "have video".
        expect = Expectation(video=False, audio=True, duration=stream.duration or info.best_duration)
        attempts = self._encode_attempts(source, info, stream, fmt, bitrate, None, expect)
        self.processor.render_first_working(output, attempts, ctx, f"Converting to {fmt.label}",
                                            progress_duration=expect.duration)
        result = self._saved(output)
        source_rate = stream.bit_rate or info.bit_rate
        if fmt.uses_bitrate and bitrate and source_rate and bitrate * 1000 > source_rate * 1.15 and not info.has_video:
            result.warnings.append(
                f"The original has a lower bitrate (about {source_rate // 1000} kbps) than the chosen "
                f"{bitrate} kbps. Converting cannot add quality that is not in the source; the file is just larger."
            )
        return result

    def change_volume(self, source: Path, output: Path, factor: float, ctx: JobContext) -> JobResult:
        if not (0.0 <= factor <= 5.0):
            raise InvalidInputError("Volume must be between 0% and 500%.")
        info, stream = self._probe_audio(source, ctx)
        fmt = self._format_for_output(output)
        expect = Expectation(audio=True, duration=info.best_duration)
        attempts = self._encode_attempts(source, info, stream, fmt, nearest_bitrate(stream.bit_rate or info.bit_rate),
                                         f"volume={factor:.4g}", expect)
        self.processor.render_first_working(output, attempts, ctx, f"Changing volume to {factor:.0%}",
                                            progress_duration=info.best_duration)
        return self._saved(output)

    def fade(self, source: Path, output: Path, fade_in: float, fade_out: float, ctx: JobContext) -> JobResult:
        info, stream = self._probe_audio(source, ctx)
        flt = fade_filter(info.best_duration, fade_in, fade_out)
        fmt = self._format_for_output(output)
        expect = Expectation(audio=True, duration=info.best_duration)
        attempts = self._encode_attempts(source, info, stream, fmt, nearest_bitrate(stream.bit_rate or info.bit_rate),
                                         flt, expect)
        self.processor.render_first_working(output, attempts, ctx, "Adding fades", progress_duration=info.best_duration)
        return self._saved(output)

    def read_tags(self, source: Path) -> dict[str, str]:
        info = self.processor.probe(source)
        tags = dict(info.tags)
        # Ogg/Opus keep tags on the audio stream rather than the container.
        audio = info.primary_audio
        if audio is not None and not any(k in tags for k, _ in EDITABLE_TAGS):
            stream_tags = self._stream_tags(source, audio.index)
            tags.update(stream_tags)
        return {key: tags.get(key, "") for key, _ in EDITABLE_TAGS}

    def _stream_tags(self, source: Path, index: int) -> dict[str, str]:
        import json
        import subprocess

        from app.utils.system import hidden_subprocess_kwargs

        try:
            result = subprocess.run(
                [str(self.processor.tools.ffprobe), "-v", "error", "-select_streams", str(index),
                 "-show_entries", "stream_tags", "-of", "json", str(source)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                stdin=subprocess.DEVNULL, **hidden_subprocess_kwargs(),
            )
            streams = json.loads(result.stdout or "{}").get("streams") or [{}]
            return {str(k).lower(): str(v) for k, v in (streams[0].get("tags") or {}).items()}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}

    def write_tags(self, source: Path, output: Path, tags: dict[str, str], ctx: JobContext) -> JobResult:
        info, stream = self._probe_audio(source, ctx)
        ext = output.suffix.lstrip(".").lower()
        metadata: list[str] = []
        for key, _label in EDITABLE_TAGS:
            value = (tags.get(key) or "").strip()
            metadata += ["-metadata", f"{key}={value}"]  # empty value removes the tag
            if ext in ("ogg", "opus", "oga"):
                metadata += ["-metadata:s:a:0", f"{key}={value}"]
        extra = ["-id3v2_version", "3"] if ext == "mp3" else []
        expect = Expectation(audio=True, duration=info.best_duration)
        attempt = Attempt("saving tags", lambda tmp: [
            "-i", str(source), "-map", "0", "-c", "copy", "-map_metadata", "0", *metadata, *extra, str(tmp)], expect)
        self.processor.render_first_working(output, [attempt], ctx, "Saving tags")
        return JobResult(f"Saved tags to {output.name}", outputs=[output])
