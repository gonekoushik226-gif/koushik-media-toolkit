"""Error types and conversion of any exception into a friendly message.

Services raise :class:`AppError` subclasses with a message written for the
user plus optional technical ``details`` (e.g. the last lines of FFmpeg's
output). Unexpected exceptions are converted by :func:`describe_exception`;
full tracebacks only ever go to the log file.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass


class AppError(Exception):
    """An error whose message can be shown to the user as-is."""

    title = "Something went wrong"

    def __init__(self, message: str, details: str = "", title: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details
        if title:
            self.title = title


class InvalidInputError(AppError):
    title = "Please check your input"


class DependencyError(AppError):
    title = "Missing component"


class ProcessingError(AppError):
    title = "Processing failed"


class DownloadError(AppError):
    title = "Download failed"


class JobCancelled(Exception):
    """Raised inside a background job when the user pressed Cancel."""


@dataclass
class ErrorReport:
    title: str
    message: str
    details: str = ""
    unexpected: bool = False


_WINERROR_DISK_FULL = {112, 39}  # ERROR_DISK_FULL, ERROR_HANDLE_DISK_FULL
_WINERROR_IN_USE = {32, 33}  # sharing / lock violation


def _os_error_report(exc: OSError) -> ErrorReport | None:
    target = exc.filename or ""
    where = f"\n\n{target}" if target else ""
    winerror = getattr(exc, "winerror", None)
    if exc.errno == errno.ENOSPC or winerror in _WINERROR_DISK_FULL:
        return ErrorReport("Disk full", f"There is not enough free disk space to save the result.{where}")
    if winerror in _WINERROR_IN_USE:
        return ErrorReport(
            "File in use",
            f"The file is being used by another program. Close that program and try again.{where}",
        )
    if isinstance(exc, PermissionError):
        return ErrorReport(
            "Permission denied",
            "Windows did not allow access to this file or folder. Choose a different "
            f"output folder, or close programs that may be using the file.{where}",
        )
    if isinstance(exc, FileNotFoundError):
        return ErrorReport("File not found", f"A file or folder could not be found.{where}")
    if isinstance(exc, FileExistsError):
        return ErrorReport("File already exists", f"A file with this name already exists.{where}")
    if isinstance(exc, IsADirectoryError):
        return ErrorReport("Invalid file", f"A folder was given where a file was expected.{where}")
    if winerror == 206 or exc.errno == errno.ENAMETOOLONG:
        return ErrorReport("Name too long", f"The file name or path is too long. Use a shorter name or folder.{where}")
    return None


def describe_exception(exc: BaseException) -> ErrorReport:
    """Convert any exception into something presentable."""
    if isinstance(exc, AppError):
        return ErrorReport(exc.title, exc.message, exc.details)
    if isinstance(exc, JobCancelled):
        return ErrorReport("Cancelled", "The operation was cancelled.")
    if isinstance(exc, MemoryError):
        return ErrorReport(
            "Not enough memory",
            "The computer ran out of memory. Try smaller files or fewer files at once.",
            unexpected=True,
        )
    if isinstance(exc, OSError):
        report = _os_error_report(exc)
        if report is not None:
            report.details = f"{type(exc).__name__}: {exc}"
            return report
    return ErrorReport(
        "Unexpected error",
        "An unexpected error occurred. Technical details were written to the log file.",
        details=f"{type(exc).__name__}: {exc}",
        unexpected=True,
    )
