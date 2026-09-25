"""Direct HTTP(S) downloads (used for PDF files).

A normal HTTP client is used on purpose: yt-dlp is meant for video sites and
is not needed for ordinary file links.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import requests

from app import APP_ID, __version__
from app.core.errors import DownloadError, InvalidInputError, JobCancelled
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.utils.filenames import build_filename, temp_sibling
from app.utils.units import human_eta, human_size, human_speed
from app.utils.urls import UrlError, filename_from_url, redact_url, validate_web_url

log = logging.getLogger(__name__)

USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {APP_ID}/{__version__}"
CHUNK_SIZE = 256 * 1024
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
PDF_MAGIC = b"%PDF-"
_SCRIPT_EXTENSIONS = {".php", ".asp", ".aspx", ".jsp", ".cgi", ".html", ".htm", ".do", ".action"}

_HTTP_MESSAGES = {
    400: "The server rejected the request (HTTP 400 Bad Request). Check the link.",
    401: "The server requires signing in (HTTP 401). This app cannot download files that need a login.",
    403: "Access to this file is forbidden (HTTP 403). It may require signing in or may not be public.",
    404: "The file was not found on the server (HTTP 404). Check that the link is correct.",
    410: "The file has been removed from the server (HTTP 410).",
    429: "The server received too many requests (HTTP 429). Wait a moment and try again.",
}


def filename_from_content_disposition(header: str | None) -> str | None:
    """Parse ``Content-Disposition`` (RFC 6266 / 5987). Returns a bare file
    name (any directory part is removed) or None."""
    if not header:
        return None
    name = None
    extended = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", header, re.IGNORECASE)
    if extended:
        charset = extended.group(1).strip() or "utf-8"
        try:
            name = unquote(extended.group(2).strip().strip('"'), encoding=charset, errors="replace")
        except LookupError:
            name = unquote(extended.group(2).strip().strip('"'))
    if not name:
        quoted = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', header, re.IGNORECASE)
        if quoted:
            # Like browsers, only \" and \\ are escapes; other backslashes stay
            # (and are treated as folder separators below).
            name = re.sub(r'\\(["\\])', r"\1", quoted.group(1))
        else:
            bare = re.search(r"filename\s*=\s*([^;]+)", header, re.IGNORECASE)
            if bare:
                name = bare.group(1).strip()
    if not name:
        return None
    name = re.split(r"[\\/]", name)[-1].strip()
    return name or None


def suggest_pdf_filename(url: str, content_disposition: str | None = None) -> str:
    """A sensible, safe ``.pdf`` file name for a download."""
    name = filename_from_content_disposition(content_disposition) or filename_from_url(url) or "document"
    stem, ext = os.path.splitext(name)
    if ext.lower() == ".pdf":
        name = stem
    elif ext.lower() in _SCRIPT_EXTENSIONS:
        name = stem  # 'download.php' -> 'download'
    return build_filename(name, "pdf", fallback="document")


def _check_status(response: requests.Response) -> None:
    code = response.status_code
    if code < 400:
        return
    message = _HTTP_MESSAGES.get(code)
    if message is None:
        message = (f"The server had an internal problem (HTTP {code}). Try again later." if code >= 500
                   else f"The server refused the download (HTTP {code}).")
    raise DownloadError(message, details=f"HTTP {code} {response.reason} - {redact_url(response.url)}")


def _translate_request_error(exc: Exception) -> DownloadError:
    if isinstance(exc, requests.exceptions.SSLError):
        return DownloadError("A secure connection to the server could not be established (certificate problem).",
                             details=str(exc))
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return DownloadError("The server did not answer in time. Check your internet connection.", details=str(exc))
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return DownloadError("The server stopped sending data. Try again later.", details=str(exc))
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return DownloadError("The link redirects too many times.", details=str(exc))
    if isinstance(exc, (requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError)):
        return DownloadError("The download was interrupted. Try again.", details=str(exc))
    if isinstance(exc, requests.exceptions.ConnectionError):
        return DownloadError("Could not connect to the server. Check your internet connection and the link.",
                             details=str(exc))
    return DownloadError("The download failed.", details=f"{type(exc).__name__}: {exc}")


def _validated(url: str) -> str:
    try:
        return validate_web_url(url)
    except UrlError as exc:
        raise InvalidInputError(str(exc)) from None


@dataclass
class LinkInfo:
    url: str  # final URL after redirects
    filename: str
    size: int | None
    content_type: str
    is_pdf: bool


def inspect_link(url: str, session: requests.Session | None = None) -> LinkInfo:
    """Fetch only the start of the file to suggest a name and check it is a PDF."""
    url = _validated(url)
    http = session or requests.Session()
    try:
        with http.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as response:
            _check_status(response)
            head = b""
            for chunk in response.iter_content(1024):
                head += chunk
                if len(head) >= 1024:
                    break
            size = _content_length(response)
            return LinkInfo(
                url=response.url,
                filename=suggest_pdf_filename(response.url, response.headers.get("Content-Disposition")),
                size=size,
                content_type=response.headers.get("Content-Type", "").split(";")[0].strip(),
                is_pdf=PDF_MAGIC in head[:1024],
            )
    except DownloadError:
        raise
    except requests.exceptions.InvalidURL as exc:
        raise InvalidInputError("This does not look like a valid web address.") from exc
    except requests.exceptions.RequestException as exc:
        raise _translate_request_error(exc) from exc


def _content_length(response: requests.Response) -> int | None:
    if response.headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
        return None  # the length refers to compressed data
    try:
        value = int(response.headers.get("Content-Length", ""))
    except ValueError:
        return None
    return value if value >= 0 else None


def _not_pdf_error(content_type: str) -> DownloadError:
    if "html" in content_type:
        return DownloadError(
            "The link opened a web page instead of a PDF file. Open the link in your browser and copy the "
            "direct link to the PDF itself.",
            details=f"Content-Type: {content_type}",
            title="Not a PDF",
        )
    return DownloadError(
        "The downloaded file is not a PDF, so it was not saved.",
        details=f"Content-Type: {content_type or 'unknown'}",
        title="Not a PDF",
    )


def download_pdf(url: str, output: Path, ctx: JobContext, session: requests.Session | None = None) -> JobResult:
    """Download ``url`` to ``output`` (overwrite permission already given).
    The data is written to a temporary file and checked before it is renamed."""
    url = _validated(url)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()
    tmp = temp_sibling(output)
    ctx.set_status("Connecting...")
    ctx.set_progress(None)
    log.info("Downloading PDF from %s", redact_url(url))
    try:
        with http.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as response:
            remove = ctx.add_cancel_callback(response.close)  # unblocks a stalled read on Cancel
            try:
                _check_status(response)
                total = _content_length(response)
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                ctx.set_status(f"Downloading {output.name}...")
                done = 0
                checked = False
                head = bytearray()
                started = time.monotonic()
                with open(tmp, "wb") as handle:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        ctx.check_cancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done += len(chunk)
                        if not checked:
                            # PDFs start with "%PDF-" within the first 1024 bytes.
                            head += chunk
                            if len(head) >= 1024:
                                if PDF_MAGIC not in head[:1024]:
                                    raise _not_pdf_error(content_type)
                                checked = True
                        elapsed = max(0.001, time.monotonic() - started)
                        speed = done / elapsed
                        eta = (total - done) / speed if total and speed else None
                        detail = f"{human_size(done)}" + (f" of {human_size(total)}" if total else "")
                        extra = " · ".join(p for p in (human_speed(speed), human_eta(eta)) if p)
                        ctx.set_progress(done / total if total else None, f"{detail} · {extra}" if extra else detail)
                if not checked and PDF_MAGIC not in bytes(head[:1024]):
                    raise _not_pdf_error(content_type)
                if total is not None and done < total:
                    raise DownloadError("The download ended early; the file is incomplete. Try again.",
                                        details=f"received {done} of {total} bytes")
            finally:
                remove()
        ctx.check_cancelled()
        ctx.set_progress(None, "Checking the PDF...")
        pages = _check_pdf(tmp)
        os.replace(tmp, output)
    except (JobCancelled, DownloadError, InvalidInputError):
        _remove(tmp)
        if ctx.is_cancelled:
            raise JobCancelled() from None
        raise
    except requests.exceptions.InvalidURL as exc:
        _remove(tmp)
        raise InvalidInputError("This does not look like a valid web address.") from exc
    except requests.exceptions.RequestException as exc:
        _remove(tmp)
        if ctx.is_cancelled:
            raise JobCancelled() from None
        raise _translate_request_error(exc) from exc
    except BaseException:
        _remove(tmp)
        if ctx.is_cancelled:
            raise JobCancelled() from None
        raise
    log.info("Saved PDF %s (%s pages)", output, pages)
    return JobResult(f"Downloaded {output.name} ({pages} page(s), {human_size(output.stat().st_size)})",
                     outputs=[output])


def _check_pdf(path: Path) -> int:
    try:
        import pymupdf

        with pymupdf.open(str(path)) as doc:
            if not doc.is_pdf or doc.page_count == 0:
                raise ValueError("no pages")
            return doc.page_count
    except Exception as exc:  # noqa: BLE001
        raise DownloadError("The downloaded file looks like a PDF but is damaged or incomplete, so it was not saved.",
                            details=str(exc), title="Damaged PDF") from exc


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not remove %s", path)
