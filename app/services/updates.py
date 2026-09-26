"""Checking whether a newer version of the app has been released on GitHub.

Only the public "latest release" information is read. Nothing personal is
sent (GitHub sees an ordinary HTTPS request), and nothing is downloaded or
installed automatically: the user is shown the release page and decides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from app import APP_ID, __version__
from app.core.errors import AppError

log = logging.getLogger(__name__)

# The project's GitHub repository, addressed by its permanent numeric id (it
# keeps working if the repository is renamed or moves to another account).
REPOSITORY_ID = 1387132160
LATEST_RELEASE_API = f"https://api.github.com/repositories/{REPOSITORY_ID}/releases/latest"
RELEASE_PAGE_PREFIX = "https://github.com/"


class UpdateCheckError(AppError):
    title = "Could not check for updates"


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # "1.2.0"
    title: str  # the release title, e.g. "Media Toolkit 1.2.0"
    page_url: str  # the release page on github.com
    published: str  # "2026-09-26" (may be empty)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """'v1.2.0' / '1.2' / 'Version 1.2.3-beta' -> (1, 2, 0) / (1, 2, 0) / (1, 2, 3)."""
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def latest_release(url: str = LATEST_RELEASE_API, session: requests.Session | None = None,
                   timeout: tuple[float, float] = (5.0, 10.0)) -> UpdateInfo | None:
    """The newest published (non-draft, non-pre-release) release, or None if
    there is none yet. Raises UpdateCheckError when GitHub cannot be asked."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": f"{APP_ID}/{__version__}"}
    try:
        response = (session or requests).get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise UpdateCheckError("Could not reach GitHub to check for a new version. Check your internet "
                               "connection and try again later.", details=str(exc)) from exc
    if response.status_code == 404:
        return None  # no release published yet
    if response.status_code in (403, 429):
        raise UpdateCheckError("GitHub is limiting requests right now. Please try again in an hour.",
                               details=f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise UpdateCheckError(f"GitHub answered with an error (HTTP {response.status_code}). Please try again "
                               "later.")
    try:
        data = response.json()
    except ValueError as exc:
        raise UpdateCheckError("GitHub sent an unreadable answer. Please try again later.") from exc
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    version = parse_version(tag)
    page = str(data.get("html_url") or "")
    if version is None or not page.startswith(RELEASE_PAGE_PREFIX):
        log.warning("Ignoring an unexpected release entry (tag %r)", tag)
        return None
    return UpdateInfo(version=".".join(str(part) for part in version), title=str(data.get("name") or tag),
                      page_url=page, published=str(data.get("published_at") or "")[:10])


def check_for_update(current: str = __version__, **kwargs) -> UpdateInfo | None:
    """The latest release if it is newer than ``current``, else None."""
    info = latest_release(**kwargs)
    if info is not None and is_newer(info.version, current):
        log.info("A newer version is available: %s (running %s)", info.version, current)
        return info
    log.info("No newer version (running %s, latest %s)", current, info.version if info else "none")
    return None
