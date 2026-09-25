"""Run FFmpeg with live progress, cancellation and readable errors."""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
from pathlib import Path

from app.core.errors import JobCancelled, ProcessingError
from app.core.jobs import JobContext
from app.utils.system import hidden_subprocess_kwargs
from app.utils.timefmt import format_duration

log = logging.getLogger(__name__)

# (substring in FFmpeg's error output, friendly explanation) - first match wins.
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("no such file or directory", "An input file could not be found. It may have been moved or deleted."),
    ("moov atom not found", "The video file is incomplete or damaged (it may still be downloading or copying)."),
    ("invalid data found when processing input", "The file is not a valid media file, or it is damaged."),
    ("permission denied", "Windows denied access to a file. Make sure the output folder is writable and the file is not open in another program."),
    ("no space left on device", "There is not enough free disk space."),
    ("does not contain any stream", "The file does not contain the needed stream (for example, it has no audio track)."),
    ("matches no streams", "The file does not contain the needed stream (for example, it has no audio track)."),
    ("output file is empty", "FFmpeg produced an empty result. The selected range may contain no data."),
    ("unknown encoder", "This FFmpeg build lacks a required encoder. Use the FFmpeg bundled with the app "
                        "(Settings > Advanced: leave the path empty)."),
    ("could not find tag for codec", "The chosen output format cannot store this kind of stream."),
    ("not currently supported in container", "The chosen output format cannot store this kind of stream."),
    ("could not write header", "The chosen output format cannot store these streams."),
    ("too many packets buffered", "The file's timing information is unusual and could not be processed."),
    ("height not divisible by 2", "The video size must be an even number of pixels."),
    ("width not divisible by 2", "The video size must be an even number of pixels."),
)


def explain_ffmpeg_error(lines: list[str]) -> str:
    text = "\n".join(lines).lower()
    for needle, message in _ERROR_HINTS:
        if needle in text:
            return message
    return "FFmpeg could not process the file."


class ProgressParser:
    """Parses ``-progress pipe:1`` output (``key=value`` lines).

    ``feed`` returns the processed media time in seconds when known.
    """

    def __init__(self) -> None:
        self.finished = False
        self.speed = ""
        self.seconds: float | None = None

    def feed(self, line: str) -> float | None:
        key, sep, value = line.strip().partition("=")
        if not sep:
            return None
        value = value.strip()
        if key in ("out_time_us", "out_time_ms"):  # both are microseconds (FFmpeg quirk)
            try:
                micros = int(value)
            except ValueError:
                return None
            if micros >= 0:
                self.seconds = micros / 1_000_000
                return self.seconds
        elif key == "speed":
            self.speed = value if value not in ("N/A", "") else ""
        elif key == "progress" and value == "end":
            self.finished = True
        return None


def build_command(ffmpeg: Path, args: list[str]) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",  # outputs are always fresh temp files; overwrites are decided earlier
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        *args,
    ]


def run_ffmpeg(ffmpeg: Path, args: list[str], ctx: JobContext, duration: float | None = None) -> None:
    """Run FFmpeg; report progress as a fraction of ``duration`` (seconds)
    when given, otherwise as indeterminate. Raises ProcessingError/JobCancelled."""
    cmd = build_command(ffmpeg, args)
    log.info("Running: %s", subprocess.list2cmdline(cmd))
    ctx.check_cancelled()
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except OSError as exc:
        raise ProcessingError(
            "FFmpeg could not be started.", details=f"{ffmpeg}: {exc}", title="FFmpeg problem"
        ) from exc

    stderr_tail: collections.deque[str] = collections.deque(maxlen=40)

    def read_stderr() -> None:
        assert process.stderr is not None
        for err_line in process.stderr:
            if err_line.strip():
                stderr_tail.append(err_line.rstrip())

    reader = threading.Thread(target=read_stderr, name="ffmpeg-stderr", daemon=True)
    reader.start()
    remove_callback = ctx.add_cancel_callback(process.kill)
    parser = ProgressParser()
    ctx.set_progress(0.0 if duration else None)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            seconds = parser.feed(line)
            if seconds is None:
                continue
            speed = f" at {parser.speed}" if parser.speed else ""
            if duration and duration > 0:
                ctx.set_progress(
                    seconds / duration, f"{format_duration(seconds)} of {format_duration(duration)}{speed}"
                )
            else:
                ctx.set_progress(None, f"{format_duration(seconds)} processed{speed}")
        process.wait()
    finally:
        remove_callback()
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=5)

    if ctx.is_cancelled:
        raise JobCancelled()
    if process.returncode != 0:
        lines = list(stderr_tail)
        log.warning("FFmpeg failed (exit code %s):\n%s", process.returncode, "\n".join(lines))
        raise ProcessingError(explain_ffmpeg_error(lines), details="\n".join(lines[-15:]))
    ctx.set_progress(1.0)
