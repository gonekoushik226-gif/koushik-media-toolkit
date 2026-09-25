"""URL validation and helpers."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_HOST_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")
_SCHEMELESS_RE = re.compile(r"^(www\.|[A-Za-z0-9\-]+\.[A-Za-z]{2,})", re.IGNORECASE)
# Query parameters that are safe to keep in log files (video ids etc.).
_LOG_SAFE_PARAMS = {"v", "list", "index", "t", "p", "page"}


class UrlError(ValueError):
    """The text is not a usable web address."""


def normalize_url(text: str) -> str:
    """Trim whitespace/quotes/angle brackets; add https:// to 'www.site.com/...'."""
    value = (text or "").strip().strip("<>\"'").strip()
    if value and "://" not in value and _SCHEMELESS_RE.match(value):
        value = "https://" + value
    return value


def validate_web_url(text: str) -> str:
    """Return a normalised http(s) URL or raise :class:`UrlError` with a
    message suitable for the user."""
    value = normalize_url(text)
    if not value:
        raise UrlError("Please enter a web address (URL).")
    if any(ch.isspace() for ch in value):
        raise UrlError("The web address contains spaces. Please paste the full link.")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise UrlError("This does not look like a valid web address.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlError("Only web addresses starting with http:// or https:// are supported.")
    host = parts.hostname or ""
    if not host or not _HOST_RE.match(host) or host.startswith(".") or ".." in host:
        raise UrlError("The web address is missing a valid site name.")
    try:
        parts.port  # noqa: B018 - raises ValueError for invalid ports
    except ValueError:
        raise UrlError("The web address has an invalid port number.") from None
    return value


def filename_from_url(url: str) -> str:
    """Last path segment, URL-decoded ('' if there is none)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(segment).strip()


def redact_url(url: str) -> str:
    """Drop query parameters that could carry tokens/signatures before logging."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    netloc = parts.netloc.rsplit("@", 1)[-1]  # never log user:password@
    if not parts.query and netloc == parts.netloc:
        return url
    all_params = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in all_params if k.lower() in _LOG_SAFE_PARAMS]
    query = urlencode(kept)
    if len(kept) != len(all_params):
        query = f"{query}&<redacted>" if query else "<redacted>"
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
