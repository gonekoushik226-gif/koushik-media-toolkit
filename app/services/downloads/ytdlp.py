"""yt-dlp integration: analysing links and downloading formats.

Two interchangeable backends produce identical results:

* :class:`EmbeddedYtDlp` - the yt-dlp library bundled with the app (default).
* :class:`ExternalYtDlp` - a ``yt-dlp.exe`` chosen in Settings. Websites
  change often; an external yt-dlp can be updated on its own (``yt-dlp -U``)
  without waiting for a new version of this app.

Downloads go into a private temporary folder inside the output folder and
are moved to their final name only when complete, so a failed or cancelled
download leaves nothing behind.
"""

from __future__ import annotations

import datetime as dt
import gc
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.config import paths
from app.core.errors import DependencyError, DownloadError, InvalidInputError, JobCancelled
from app.core.jobs import JobContext, run_abandonable
from app.models.remote import RemoteMedia, parse_info_dict
from app.services.downloads.plans import DownloadPlan
from app.services.tools import ToolLocator, find_js_runtimes, tool_version
from app.utils.filenames import PARTIAL_MARKER, same_path
from app.utils.system import hidden_subprocess_kwargs, kill_process_tree
from app.utils.units import human_eta, human_size, human_speed
from app.utils.urls import UrlError, validate_web_url

log = logging.getLogger(__name__)

WORK_DIR_PREFIX = ".mt-download-"
ANALYZE_FORMAT = "bv*+ba/b/bv*/ba*"  # always matches something if any format exists
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEFTOVER_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")

# (regular expression on the lower-cased message, friendly explanation) - first match wins.
_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"unsupported url", "This website or link is not supported."),
    (r"is not a valid url", "This is not a valid link."),
    (r"\bdrm\b", "This content is protected with DRM (copy protection) and cannot be downloaded."),
    (r"private video", "This video is private."),
    (r"members[- ]only|join this channel", "This video is only available to paying channel members."),
    (r"confirm your age|age[- ]restricted|inappropriate for some users",
     "This video is age-restricted and requires signing in, which this app does not support."),
    (r"not a bot|sign in to confirm",
     "The website asked to confirm you are not a bot. Try again later; signed-in downloads are not supported."),
    (r"login required|requires authentication|--cookies|use --username|please log ?in|you need to log ?in|sign in to view",
     "This content requires signing in, which this app does not support."),
    (r"live event will begin|premieres in|is_upcoming", "This live stream or premiere has not started yet."),
    (r"video unavailable|this video is not available|not available in your country|geo.?restrict",
     "This video is unavailable. It may have been removed or blocked in your country."),
    (r"\bffmpeg\b|\bffprobe\b", "FFmpeg is required for this download but could not be used."),
    (r"http error 403|403: forbidden", "The website refused access (HTTP 403 Forbidden)."),
    (r"http error 404|404: not found", "The page or file was not found (HTTP 404)."),
    (r"http error 429|too many requests", "The website received too many requests. Please wait and try again later."),
    (r"http error 5\d\d", "The website's server had an error. Please try again later."),
    (r"requested format (is )?not available|format is not available",
     "The selected format is no longer available. Please analyze the link again."),
    (r"no video formats found|no formats found", "No downloadable media was found at this link."),
    (r"getaddrinfo failed|name or service not known|nodename nor servname|failed to resolve|"
     r"no address associated|network is unreachable|errno 11001",
     "The website could not be reached. Check your internet connection."),
    (r"timed out|timeout", "The connection timed out. Check your internet connection and try again."),
    (r"certificate verify failed|sslerror|ssl: ", "A secure connection to the website could not be established."),
    (r"no space left", "There is not enough free disk space."),
    (r"permission denied|access is denied", "Windows denied access to the download folder."),
    (r"unable to download webpage|unable to download api|unable to extract",
     "The website could not be read. The link may be wrong, or the site changed and yt-dlp needs an update."),
)


def clean_ytdlp_message(message: str) -> str:
    text = _ANSI_RE.sub("", str(message or "")).strip()
    return re.sub(r"^(ERROR|WARNING):\s*", "", text)


def explain_ytdlp_error(message: str) -> str:
    text = clean_ytdlp_message(message).lower()
    for pattern, friendly in _ERROR_PATTERNS:
        if re.search(pattern, text):
            return friendly
    return "The link could not be processed by yt-dlp."


def remove_tree(folder: Path, attempts: int = 15) -> None:
    """Delete a temporary folder. On Windows a file can stay locked for a
    moment after a cancelled download (the downloader's handle is released
    by garbage collection), so retry briefly instead of leaving junk behind."""
    for _ in range(attempts):
        if not folder.exists():
            return
        try:
            shutil.rmtree(folder)
            return
        except OSError:
            gc.collect()
            time.sleep(0.2)
    log.warning("Could not remove temporary folder %s", folder)


def check_url(url: str) -> str:
    try:
        return validate_web_url(url)
    except UrlError as exc:
        raise InvalidInputError(str(exc)) from None


def media_from_info(info: dict, url: str) -> RemoteMedia:
    """Validate an info dict (single video, not live, has formats) and parse it."""
    kind = info.get("_type")
    if kind in ("playlist", "multi_video"):
        count = info.get("playlist_count") or len(info.get("entries") or [])
        raise InvalidInputError(
            f"This link is a playlist{f' with {count} items' if count else ''}. Please open one video and "
            "copy the link of that single video.",
            title="Playlist link",
        )
    live_status = info.get("live_status")
    if live_status == "is_upcoming":
        raise InvalidInputError("This live stream or premiere has not started yet.")
    if info.get("is_live") or live_status == "is_live":
        raise InvalidInputError("This is a live stream that is still running. Live streams cannot be downloaded while they are live.")
    media = parse_info_dict(info, url)
    if not media.formats:
        if media.drm_formats_skipped:
            raise DownloadError("This content is protected with DRM (copy protection) and cannot be downloaded.")
        raise DownloadError("No downloadable formats were found at this link.")
    return media


@dataclass
class DownloadTarget:
    folder: Path
    stem: str  # already sanitised file name without extension
    approved_overwrite: Path | None = None  # the user already agreed to replace this file


def ytdlp_version_age_days(version: str) -> int | None:
    """yt-dlp versions are dates ('2026.08.19'); returns the age in days."""
    match = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version or "")
    if not match:
        return None
    try:
        released = dt.date(*(int(g) for g in match.groups()))
    except ValueError:
        return None
    return (dt.date.today() - released).days


class YtDlpBackend(ABC):
    name = "yt-dlp"

    def __init__(self, ffmpeg: Path | None, js_runtimes: dict[str, Path], embed_metadata: bool = True):
        self.ffmpeg = ffmpeg
        self.js_runtimes = js_runtimes
        self.embed_metadata = embed_metadata

    @abstractmethod
    def version(self) -> str: ...

    @abstractmethod
    def analyze(self, url: str, ctx: JobContext) -> RemoteMedia: ...

    @abstractmethod
    def _download_into(self, url: str, plan: DownloadPlan, work_dir: Path, stem: str, ctx: JobContext) -> Path | None:
        """Download into ``work_dir``; return the produced file if known."""

    def download(self, url: str, plan: DownloadPlan, target: DownloadTarget, ctx: JobContext) -> Path:
        url = check_url(url)
        needs_ffmpeg = bool(plan.merge_container or plan.audio_target or self.embed_metadata)
        if needs_ffmpeg and self.ffmpeg is None:
            raise DependencyError(
                "FFmpeg is needed to combine video and audio or convert audio, but it could not be found. "
                "Open Diagnostics for details."
            )
        target.folder.mkdir(parents=True, exist_ok=True)
        work_dir = target.folder / f"{WORK_DIR_PREFIX}{secrets.token_hex(4)}"
        work_dir.mkdir()
        try:
            ctx.set_status("Starting download...")
            ctx.set_progress(None)
            produced = self._download_into(url, plan, work_dir, target.stem, ctx)
            ctx.check_cancelled()
            produced = produced if produced and produced.is_file() else self._find_output(work_dir)
            return self._move_into_place(produced, target, ctx)
        finally:
            remove_tree(work_dir)

    @staticmethod
    def _find_output(work_dir: Path) -> Path:
        files = [
            p for p in work_dir.iterdir()
            if p.is_file() and not p.name.endswith(_LEFTOVER_SUFFIXES) and PARTIAL_MARKER not in p.name
        ]
        if not files:
            raise DownloadError("The download finished, but no file was produced.")
        return max(files, key=lambda p: p.stat().st_size)

    @staticmethod
    def _move_into_place(produced: Path, target: DownloadTarget, ctx: JobContext) -> Path:
        final = target.folder / f"{target.stem}{produced.suffix.lower()}"
        if final.exists() and not (target.approved_overwrite and same_path(final, target.approved_overwrite)):
            final = ctx.resolve_outputs([final])[0]
        os.replace(produced, final)
        log.info("Download saved as %s", final)
        return final

    # Shared progress formatting --------------------------------------------
    @staticmethod
    def _stream_label(plan: DownloadPlan, format_id: str) -> str:
        if len(plan.format_ids) == 2 and format_id in plan.format_ids:
            index = plan.format_ids.index(format_id)
            return f"{'video' if index == 0 else 'audio'} ({index + 1} of 2)"
        return ""

    @staticmethod
    def _report_download(ctx: JobContext, done: float | None, total: float | None, exact_total: bool,
                         speed: float | None, eta: float | None, frag_index: float | None,
                         frag_count: float | None) -> None:
        fraction = (done / total) if (done is not None and total) else None
        if fraction is None and frag_count:
            fraction = (frag_index or 0) / frag_count
        size = human_size(done) if done is not None else ""
        if total:
            size += f" of {'' if exact_total else '~'}{human_size(total)}"
        parts = [p for p in (size, human_speed(speed), human_eta(eta)) if p]
        ctx.set_progress(fraction, " · ".join(parts))

    @staticmethod
    def _postprocess_status(name: str) -> str:
        return {
            "Merger": "Combining video and audio...",
            "FFmpegMerger": "Combining video and audio...",
            "ExtractAudio": "Converting audio...",
            "FFmpegExtractAudio": "Converting audio...",
            "Metadata": "Writing title and artist tags...",
            "FFmpegMetadata": "Writing title and artist tags...",
        }.get(name, "Finishing...")


# ----------------------------------------------------------------------------
# Embedded library backend
# ----------------------------------------------------------------------------
class _YtdlpLogger:
    """Routes yt-dlp messages into our log (the log formatter redacts URLs)."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", clean_ytdlp_message(msg))

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", clean_ytdlp_message(msg))

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", clean_ytdlp_message(msg))

    def error(self, msg: str) -> None:
        cleaned = clean_ytdlp_message(msg)
        log.error("yt-dlp: %s", cleaned)
        self.errors.append(cleaned)


class EmbeddedYtDlp(YtDlpBackend):
    name = "built-in yt-dlp"

    def version(self) -> str:
        from yt_dlp.version import __version__

        return __version__

    def _options(self, logger: _YtdlpLogger) -> dict:
        options: dict = {
            "logger": logger,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "noplaylist": True,
            "color": {"stdout": "no_color", "stderr": "no_color"},
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_retries": 2,
            "windowsfilenames": True,
            "overwrites": True,
            "continuedl": False,
            "updatetime": False,
            "cachedir": str(paths.data_dir() / "cache" / "yt-dlp"),
        }
        if self.ffmpeg:
            options["ffmpeg_location"] = str(self.ffmpeg)
        if self.js_runtimes:
            options["js_runtimes"] = {name: {"path": str(path)} for name, path in self.js_runtimes.items()}
        return options

    def analyze(self, url: str, ctx: JobContext) -> RemoteMedia:
        url = check_url(url)
        import yt_dlp

        logger = _YtdlpLogger()
        options = self._options(logger) | {
            "skip_download": True,
            "extract_flat": "in_playlist",
            "format": ANALYZE_FORMAT,
            "merge_output_format": "mp4/webm/mkv",
        }

        def work() -> dict:
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))

        ctx.set_status("Analyzing link...")
        ctx.set_progress(None, "Asking the website which formats are available")
        try:
            info = run_abandonable(work, ctx)
        except JobCancelled:
            raise
        except yt_dlp.utils.DownloadError as exc:
            message = clean_ytdlp_message(str(exc))
            raise DownloadError(explain_ytdlp_error(message), details=message, title="Could not analyze the link") from exc
        except Exception as exc:  # noqa: BLE001 - extractor bugs surface as arbitrary exceptions
            log.exception("yt-dlp analysis crashed")
            raise DownloadError("The link could not be analyzed.", details=f"{type(exc).__name__}: {exc}",
                                title="Could not analyze the link") from exc
        return media_from_info(info, url)

    def _download_into(self, url: str, plan: DownloadPlan, work_dir: Path, stem: str, ctx: JobContext) -> Path | None:
        import yt_dlp

        class Cancelled(yt_dlp.utils.DownloadCancelled):
            msg = "Cancelled by the user"

        logger = _YtdlpLogger()
        state = {"format": None}

        def progress_hook(data: dict) -> None:
            if ctx.is_cancelled:
                raise Cancelled()
            status = data.get("status")
            if status == "downloading":
                format_id = str((data.get("info_dict") or {}).get("format_id") or "")
                if format_id != state["format"]:
                    state["format"] = format_id
                    label = self._stream_label(plan, format_id)
                    ctx.set_status(f"Downloading {label}..." if label else "Downloading...")
                total = data.get("total_bytes")
                self._report_download(ctx, data.get("downloaded_bytes"), total or data.get("total_bytes_estimate"),
                                      bool(total), data.get("speed"), data.get("eta"),
                                      data.get("fragment_index"), data.get("fragment_count"))
            elif status == "finished":
                ctx.set_progress(1.0, "Download complete")

        def postprocessor_hook(data: dict) -> None:
            if ctx.is_cancelled:
                raise Cancelled()
            if data.get("status") == "started":
                ctx.set_status(self._postprocess_status(str(data.get("postprocessor") or "")))
                ctx.set_progress(None)

        postprocessors: list[dict] = []
        if plan.audio_target:
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": plan.audio_target,
                "preferredquality": str(plan.audio_bitrate) if plan.audio_bitrate else None,
            })
        if self.embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False})
        options = self._options(logger) | {
            "format": plan.format_selector,
            "outtmpl": {"default": str(work_dir / (stem.replace("%", "%%") + ".%(ext)s"))},
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "postprocessors": postprocessors,
        }
        if plan.merge_container:
            options["merge_output_format"] = plan.merge_container
        # Errors are recorded and raised *after* the except blocks: the original
        # exception's traceback keeps yt-dlp's frames - and their open .part
        # file - alive, which would stop the temporary folder being deleted.
        info: dict | None = None
        failure: str | None = None
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except (Cancelled, JobCancelled, yt_dlp.utils.DownloadCancelled):
            ctx.cancel()
        except yt_dlp.utils.DownloadError as exc:
            failure = clean_ytdlp_message(str(exc))
        gc.collect()  # close file handles held by the discarded frames
        if ctx.is_cancelled:
            raise JobCancelled()
        if failure is not None:
            raise DownloadError(explain_ytdlp_error(failure), details=failure)
        for item in (info or {}).get("requested_downloads") or []:
            filepath = item.get("filepath")
            if filepath and Path(filepath).is_file():
                return Path(filepath)
        return None


# ----------------------------------------------------------------------------
# External yt-dlp.exe backend
# ----------------------------------------------------------------------------
_PROGRESS_PREFIX = "MTPROG "
_POSTPROCESS_PREFIX = "MTPOST "
_FILE_PREFIX = "MTFILE "


def _na_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # yt-dlp prints "NA" for missing values


class ExternalYtDlp(YtDlpBackend):
    name = "external yt-dlp"

    def __init__(self, executable: Path, ffmpeg: Path | None, js_runtimes: dict[str, Path], embed_metadata: bool = True):
        super().__init__(ffmpeg, js_runtimes, embed_metadata)
        self.executable = executable

    def version(self) -> str:
        return tool_version(self.executable, ("--version",))

    def _common_args(self) -> list[str]:
        args = [
            "--no-playlist", "--color", "never", "--encoding", "utf-8", "--socket-timeout", "30",
            "--retries", "5", "--fragment-retries", "5", "--windows-filenames", "--no-mtime",
            "--cache-dir", str(paths.data_dir() / "cache" / "yt-dlp"),
        ]
        if self.ffmpeg:
            args += ["--ffmpeg-location", str(self.ffmpeg)]
        for name, path in self.js_runtimes.items():
            args += ["--js-runtimes", f"{name}:{path}"]
        return args

    def _popen(self, args: list[str]) -> subprocess.Popen:
        cmd = [str(self.executable), *args]
        log.info("Running external yt-dlp: %s", subprocess.list2cmdline(cmd))
        try:
            return subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            raise DependencyError(f"The yt-dlp program could not be started:\n{self.executable}", details=str(exc)) from exc

    def analyze(self, url: str, ctx: JobContext) -> RemoteMedia:
        url = check_url(url)
        ctx.set_status("Analyzing link...")
        ctx.set_progress(None, "Asking the website which formats are available")
        process = self._popen([*self._common_args(), "--flat-playlist", "-f", ANALYZE_FORMAT,
                               "--merge-output-format", "mp4/webm/mkv", "-J", "--", url])
        remove = ctx.add_cancel_callback(lambda: kill_process_tree(process))
        try:
            stdout, stderr = process.communicate()
        finally:
            remove()
        if ctx.is_cancelled:
            raise JobCancelled()
        if process.returncode != 0:
            message = clean_ytdlp_message(stderr.strip().splitlines()[-1] if stderr.strip() else "")
            raise DownloadError(explain_ytdlp_error(stderr), details=message or stderr[-2000:],
                                title="Could not analyze the link")
        try:
            info = json.loads(stdout)
        except ValueError as exc:
            raise DownloadError("yt-dlp returned unreadable information.", details=stdout[:500]) from exc
        return media_from_info(info, url)

    def _download_into(self, url: str, plan: DownloadPlan, work_dir: Path, stem: str, ctx: JobContext) -> Path | None:
        template = str(work_dir / (stem.replace("%", "%%") + ".%(ext)s"))
        args = [
            *self._common_args(), "-f", plan.format_selector, "-o", template, "--newline", "--no-simulate",
            "--progress",
            "--progress-template",
            f"download:{_PROGRESS_PREFIX}%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
            "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(info.format_id)s|"
            "%(progress.fragment_index)s|%(progress.fragment_count)s",
            "--progress-template", f"postprocess:{_POSTPROCESS_PREFIX}%(progress.status)s|%(progress.postprocessor)s",
            "--print", f"after_move:{_FILE_PREFIX}%(filepath)s",
        ]
        if plan.merge_container:
            args += ["--merge-output-format", plan.merge_container]
        if plan.audio_target:
            args += ["-x", "--audio-format", plan.audio_target]
            if plan.audio_bitrate:
                args += ["--audio-quality", f"{plan.audio_bitrate}K"]
        if self.embed_metadata:
            args += ["--embed-metadata", "--no-embed-chapters"]
        args += ["--", url]

        process = self._popen(args)
        stderr_lines: list[str] = []

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                if line.strip():
                    stderr_lines.append(clean_ytdlp_message(line))

        reader = threading.Thread(target=read_stderr, name="ytdlp-stderr", daemon=True)
        reader.start()
        remove = ctx.add_cancel_callback(lambda: kill_process_tree(process))
        produced: Path | None = None
        current_format = None
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\r\n")
                if line.startswith(_PROGRESS_PREFIX):
                    fields = line[len(_PROGRESS_PREFIX):].split("|")
                    if len(fields) < 9:
                        continue
                    status, done, total, estimate, speed, eta, format_id, frag_i, frag_n = fields[:9]
                    if status == "downloading":
                        if format_id != current_format:
                            current_format = format_id
                            label = self._stream_label(plan, format_id)
                            ctx.set_status(f"Downloading {label}..." if label else "Downloading...")
                        exact = _na_float(total)
                        self._report_download(ctx, _na_float(done), exact or _na_float(estimate), bool(exact),
                                              _na_float(speed), _na_float(eta), _na_float(frag_i), _na_float(frag_n))
                    elif status == "finished":
                        ctx.set_progress(1.0, "Download complete")
                elif line.startswith(_POSTPROCESS_PREFIX):
                    status, _, name = line[len(_POSTPROCESS_PREFIX):].partition("|")
                    if status == "started":
                        ctx.set_status(self._postprocess_status(name))
                        ctx.set_progress(None)
                elif line.startswith(_FILE_PREFIX):
                    produced = Path(line[len(_FILE_PREFIX):].strip())
            process.wait()
        finally:
            remove()
            if process.poll() is None:
                kill_process_tree(process)
            reader.join(timeout=5)
        if ctx.is_cancelled:
            raise JobCancelled()
        if process.returncode != 0:
            errors = [line for line in stderr_lines if "error" in line.lower()] or stderr_lines
            message = errors[-1] if errors else f"yt-dlp exited with code {process.returncode}"
            raise DownloadError(explain_ytdlp_error(message), details="\n".join(stderr_lines[-15:]))
        return produced


def get_backend(locator: ToolLocator, embed_metadata: bool = True) -> YtDlpBackend:
    ffmpeg = locator.ffmpeg().path
    runtimes = find_js_runtimes()
    external = locator.ytdlp_executable()
    if external is None:
        return EmbeddedYtDlp(ffmpeg, runtimes, embed_metadata)
    if external.path is None:
        raise DependencyError(
            f"The yt-dlp program chosen in Settings was not found:\n{external.configured_but_missing}\n\n"
            "Fix the path in Settings > Advanced, or clear it to use the yt-dlp built into the app."
        )
    return ExternalYtDlp(external.path, ffmpeg, runtimes, embed_metadata)
