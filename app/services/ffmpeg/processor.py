"""Safe execution of FFmpeg jobs.

Every output is written to a temporary file next to the destination, checked
with ffprobe (expected streams present, sensible duration) and only then
renamed to the requested name. A failed or cancelled job therefore never
leaves a broken file under the name the user asked for.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import JobCancelled, ProcessingError
from app.core.jobs import JobContext
from app.models.media import MediaInfo
from app.services.ffmpeg.probe import probe
from app.services.ffmpeg.runner import run_ffmpeg
from app.services.tools import MediaTools
from app.utils.filenames import temp_sibling

log = logging.getLogger(__name__)

ArgsBuilder = Callable[[Path], list[str]]  # temp output path -> ffmpeg arguments


@dataclass
class Expectation:
    """What a correct output must look like."""

    video: bool | None = None  # True: must have video; False: must not
    audio: bool | None = None
    duration: float | None = None  # expected length in seconds
    tolerance: float | None = None  # allowed difference; default max(1.5 s, 3 %)


@dataclass
class Attempt:
    label: str  # e.g. "fast copy" / "re-encode" (shown in the status line)
    build: ArgsBuilder
    expect: Expectation


def remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not remove temporary file %s", path)


class MediaProcessor:
    def __init__(self, tools: MediaTools):
        self.tools = tools

    def probe(self, path: Path) -> MediaInfo:
        return probe(self.tools.ffprobe, Path(path))

    def verify(self, path: Path, expect: Expectation) -> MediaInfo:
        if not path.is_file() or path.stat().st_size == 0:
            raise ProcessingError("FFmpeg did not produce an output file.")
        try:
            info = self.probe(path)
        except ProcessingError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable result is a failed result
            raise ProcessingError("The result could not be verified and was discarded.", details=str(exc)) from exc
        if expect.video is True and not info.has_video:
            raise ProcessingError("The result has no video stream and was discarded.")
        if expect.video is False and info.has_video:
            raise ProcessingError("The result unexpectedly contains video and was discarded.")
        if expect.audio is True and not info.has_audio:
            raise ProcessingError("The result has no audio stream and was discarded.")
        if expect.audio is False and info.has_audio:
            raise ProcessingError("The result unexpectedly contains audio and was discarded.")
        if expect.duration:
            actual = info.best_duration
            tolerance = expect.tolerance if expect.tolerance is not None else max(1.5, expect.duration * 0.03)
            if actual is None or abs(actual - expect.duration) > tolerance:
                raise ProcessingError(
                    "The result has the wrong length and was discarded.",
                    details=f"expected about {expect.duration:.2f}s, got {actual if actual is not None else 'unknown'}",
                )
        return info

    def render(
        self,
        output: Path,
        build: ArgsBuilder,
        ctx: JobContext,
        expect: Expectation | None = None,
        progress_duration: float | None = None,
        cwd: Path | None = None,
    ) -> MediaInfo:
        """Run FFmpeg into a temp file, verify it, then move it to ``output``
        (replacing an existing file - overwrite permission is checked before
        the job starts)."""
        expect = expect or Expectation()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_sibling(output)
        try:
            run_ffmpeg(self.tools.ffmpeg, build(tmp), ctx, duration=progress_duration or expect.duration, cwd=cwd)
            ctx.check_cancelled()
            ctx.set_progress(None, "Checking the result...")
            info = self.verify(tmp, expect)
            os.replace(tmp, output)
            info.path = output
            log.info("Created %s", output)
            return info
        except BaseException:
            remove_quietly(tmp)
            raise

    def render_first_working(
        self,
        output: Path,
        attempts: list[Attempt],
        ctx: JobContext,
        status: str,
        progress_duration: float | None = None,
        cwd: Path | None = None,
    ) -> tuple[MediaInfo, str]:
        """Try strategies in order (typically: stream copy, then re-encode).
        Returns the result and the label of the strategy that worked."""
        if not attempts:
            raise ValueError("no attempts")
        last_error: ProcessingError | None = None
        for number, attempt in enumerate(attempts):
            ctx.set_status(f"{status} ({attempt.label})" if len(attempts) > 1 else status)
            try:
                info = self.render(output, attempt.build, ctx, attempt.expect, progress_duration, cwd)
                return info, attempt.label
            except JobCancelled:
                raise
            except ProcessingError as exc:
                last_error = exc
                if number < len(attempts) - 1:
                    log.info("Strategy '%s' failed (%s); trying the next one", attempt.label, exc.message)
        assert last_error is not None
        raise last_error
