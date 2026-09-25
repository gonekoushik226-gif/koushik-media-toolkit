"""Application logging: a rotating log file with sensitive data redacted."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

from app.utils.urls import redact_url

LOG_FILE_NAME = "koushik-media-toolkit.log"

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_COOKIE_RE = re.compile(r"(?im)\b(cookie|set-cookie)(\s*:\s*)(.+)$")
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|password|passwd|pwd|token|access_token|api[_-]?key|secret)"
    r"(\s*[:=]\s*)((?:bearer|basic|token)\s+)?(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact(text: str) -> str:
    """Remove URL query strings (signed links, tokens) and obvious secrets."""
    text = _URL_RE.sub(lambda m: redact_url(m.group(0)), text)
    text = _COOKIE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}<redacted>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_folder: Path, debug: bool = False, console: bool = False) -> Path:
    """Configure the root logger. Returns the log file path."""
    log_folder.mkdir(parents=True, exist_ok=True)
    log_file = log_folder / LOG_FILE_NAME
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # Third-party libraries can be chatty at INFO level.
    for noisy in ("PIL", "urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("thread").error(
            "Unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook
    return log_file
