"""Self-checks shown on the Diagnostics screen."""

from __future__ import annotations

import logging
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app import APP_NAME, __version__
from app.config import paths
from app.services.tools import SOURCE_PATH, ToolInfo, ToolLocator, find_js_runtimes, tool_version

log = logging.getLogger(__name__)

OK, WARNING, ERROR, INFO = "ok", "warning", "error", "info"
_CONNECTIVITY_URLS = ("https://www.gstatic.com/generate_204", "https://example.com", "https://pypi.org")
YTDLP_OUTDATED_DAYS = 90


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: str = ""
    fix: str = ""


def _short_version(line: str) -> str:
    return line.split(" Copyright")[0].strip() if line else ""


def _tool_check(label: str, tool: ToolInfo, purpose: str) -> Check:
    if not tool.found:
        return Check(label, ERROR, "Missing", details=f"{label} is needed for {purpose}.",
                     fix=(f"Reinstall the application (the installer and portable EXE include {label}), or install "
                          "FFmpeg (winget install Gyan.FFmpeg) and enter the path of "
                          f"{tool.name}.exe in Settings > Advanced."))
    version = _short_version(tool_version(tool.path))  # type: ignore[arg-type]
    if not version:
        return Check(label, ERROR, "Found but does not run", details=str(tool.path),
                     fix=f"The file may be damaged or blocked. Reinstall the application or choose another {tool.name}.exe.")
    details = f"{tool.path}\nSource: {tool.source}"
    if tool.configured_but_missing:
        return Check(label, WARNING, version, details=details + f"\nThe path set in Settings was not found: "
                     f"{tool.configured_but_missing}", fix="Correct or clear the path in Settings > Advanced.")
    if tool.source == SOURCE_PATH:
        return Check(label, OK, f"{version} (from system PATH)", details=details)
    return Check(label, OK, version, details=details)


def _ytdlp_check(locator: ToolLocator) -> Check:
    from app.services.downloads.ytdlp import ytdlp_version_age_days

    external = locator.ytdlp_executable()
    if external is not None:
        if not external.found:
            return Check("yt-dlp", ERROR, "The external yt-dlp set in Settings was not found",
                         details=external.configured_but_missing,
                         fix="Correct the path in Settings > Advanced, or clear it to use the built-in yt-dlp.")
        version = tool_version(external.path, ("--version",))  # type: ignore[arg-type]
        if not version:
            return Check("yt-dlp", ERROR, "External yt-dlp does not run", details=str(external.path),
                         fix="Choose a working yt-dlp.exe in Settings > Advanced, or clear the path.")
        where = f"external: {external.path}"
    else:
        try:
            from yt_dlp.version import __version__ as version
        except Exception as exc:  # noqa: BLE001
            return Check("yt-dlp", ERROR, "The built-in yt-dlp could not be loaded", details=str(exc),
                         fix="Reinstall the application.")
        where = "built into the application"
    age = ytdlp_version_age_days(version)
    if age is not None and age > YTDLP_OUTDATED_DAYS:
        return Check("yt-dlp", WARNING, f"{version} ({where}) - {age} days old",
                     details="Websites change often; an old yt-dlp may fail on some sites.",
                     fix=("Download the latest yt-dlp.exe from https://github.com/yt-dlp/yt-dlp/releases, save it "
                          "somewhere permanent and select it in Settings > Advanced (it can then update itself "
                          "with 'yt-dlp -U')."))
    return Check("yt-dlp", OK, f"{version} ({where})")


def _js_runtime_check() -> Check:
    runtimes = find_js_runtimes()
    if runtimes:
        names = ", ".join(f"{name} ({path})" for name, path in runtimes.items())
        return Check("JavaScript runtime (for YouTube)", OK, ", ".join(runtimes), details=names)
    return Check("JavaScript runtime (for YouTube)", WARNING, "Not found",
                 details="yt-dlp needs a JavaScript runtime to unlock all YouTube formats. Other sites are not affected.",
                 fix="Install Deno (free): open a terminal and run 'winget install DenoLand.Deno', then restart the app.")


def _library_checks() -> list[Check]:
    checks = []
    try:
        import PIL
        from PIL import features

        webp = "WEBP supported" if features.check("webp") else "no WEBP support"
        checks.append(Check("Pillow (images)", OK, f"{PIL.__version__}, {webp}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Pillow (images)", ERROR, "Not available", details=str(exc), fix="Reinstall the application."))
    try:
        import pypdf

        checks.append(Check("pypdf (PDF editing)", OK, pypdf.__version__))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("pypdf (PDF editing)", ERROR, "Not available", details=str(exc), fix="Reinstall the application."))
    try:
        import pymupdf

        checks.append(Check("PyMuPDF (PDF rendering)", OK, f"{pymupdf.VersionBind} (MuPDF {pymupdf.VersionFitz})"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("PyMuPDF (PDF rendering)", ERROR, "Not available", details=str(exc),
                            fix="Reinstall the application."))
    try:
        import requests

        checks.append(Check("requests (PDF downloads)", OK, requests.__version__))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("requests (PDF downloads)", ERROR, "Not available", details=str(exc),
                            fix="Reinstall the application."))
    return checks


def _writable_check(label: str, folder: Path) -> Check:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".mt-write-test-", delete=True):
            pass
    except OSError as exc:
        return Check(label, ERROR, "Not writable", details=f"{folder}\n{exc}",
                     fix="Choose a different folder in Settings, or check the folder's permissions.")
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    if free is not None and free < 1024**3:
        return Check(label, WARNING, f"Writable, but only {free / 1024**2:.0f} MB free", details=str(folder),
                     fix="Free up disk space or choose a folder on another drive.")
    free_text = f", {free / 1024**3:.1f} GB free" if free is not None else ""
    return Check(label, OK, f"Writable{free_text}", details=str(folder))


def _internet_check() -> Check:
    import requests

    errors = []
    for url in _CONNECTIVITY_URLS:
        try:
            response = requests.head(url, timeout=6, allow_redirects=True)
            if response.status_code < 500:
                return Check("Internet connection", OK, "Connected", details=f"Reached {url}")
        except requests.RequestException as exc:
            errors.append(f"{url}: {type(exc).__name__}")
    return Check("Internet connection", WARNING, "No connection", details="\n".join(errors),
                 fix="Downloads need internet access. Check your network, proxy or firewall settings.")


def run_diagnostics(settings, locator: ToolLocator, include_network: bool = True, qt_version: str = "",
                    translation_key_status: str = "") -> list[Check]:
    mode = "installed/packaged" if paths.is_frozen() else "running from source"
    checks = [
        Check("Application", INFO, f"{APP_NAME} {__version__}",
              details=f"{mode}\nPython {platform.python_version()} ({sys.executable})\n"
                      f"{platform.system()} {platform.release()} ({platform.version()})"),
    ]
    if qt_version:
        checks.append(Check("Qt (user interface)", OK, qt_version))
    checks.append(_tool_check("FFmpeg", locator.ffmpeg(), "all video and audio features"))
    checks.append(_tool_check("FFprobe", locator.ffprobe(), "reading video and audio information"))
    checks.append(_ytdlp_check(locator))
    checks.append(_js_runtime_check())
    checks.extend(_library_checks())
    if translation_key_status:
        model = getattr(settings, "translation_model", "") or "default model"
        checks.append(Check("AI translation", INFO, translation_key_status,
                            details=f"Provider: Google Gemini API ({model}). Each user uses their own API key; "
                                    "only a masked preview of the key is shown."))
    checks.append(_writable_check("Download folder", settings.effective_download_dir()))
    if settings.output_dir:
        checks.append(_writable_check("Default output folder", Path(settings.output_dir)))
    checks.append(_writable_check("Settings folder", paths.data_dir()))
    checks.append(_writable_check("Log folder", paths.log_dir()))
    if include_network:
        checks.append(_internet_check())
    return checks
