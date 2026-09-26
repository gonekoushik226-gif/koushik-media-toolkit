"""Update check against a local mock of the GitHub releases API (no network)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services import updates


class MockGitHub:
    def __init__(self):
        self.response: tuple[int, object] = (404, {"message": "Not Found"})
        self.requests: list[dict] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802 - http.server API
                mock.requests.append({"path": self.path, "headers": dict(self.headers)})
                status, payload = mock.response
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/repositories/1/releases/latest"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def github():
    mock = MockGitHub()
    yield mock
    mock.close()


def release(tag="v1.2.0", **extra) -> dict:
    data = {"tag_name": tag, "name": f"Media Toolkit {tag.lstrip('v')}", "draft": False, "prerelease": False,
            "html_url": f"https://github.com/example/app/releases/tag/{tag}", "published_at": "2026-09-26T10:00:00Z"}
    data.update(extra)
    return data


@pytest.mark.parametrize("text, expected", [("v1.2.0", (1, 2, 0)), ("1.10", (1, 10, 0)), ("Version 2.0.3-beta", (2, 0, 3)),
                                            ("", None), ("latest", None)])
def test_parse_version(text, expected):
    assert updates.parse_version(text) == expected


def test_is_newer():
    assert updates.is_newer("v1.2.0", "1.1.0") and updates.is_newer("1.10.0", "1.9.9")
    assert not updates.is_newer("v1.1.0", "1.1.0") and not updates.is_newer("1.0.9", "1.1.0")
    assert not updates.is_newer("nonsense", "1.0.0")


def test_newer_release_is_reported(github):
    github.response = (200, release("v1.2.0"))
    info = updates.check_for_update("1.1.0", url=github.url)
    assert info == updates.UpdateInfo("1.2.0", "Media Toolkit 1.2.0", "https://github.com/example/app/releases/tag/v1.2.0",
                                      "2026-09-26")
    headers = github.requests[0]["headers"]
    assert headers["Accept"] == "application/vnd.github+json" and headers["User-Agent"].startswith("MediaToolkit/")
    assert "Authorization" not in headers and "Cookie" not in headers  # nothing personal is sent


def test_same_or_older_release_is_not_reported(github):
    github.response = (200, release("v1.1.0"))
    assert updates.check_for_update("1.1.0", url=github.url) is None
    assert updates.check_for_update("1.2.0", url=github.url) is None


def test_no_release_yet(github):
    github.response = (404, {"message": "Not Found"})
    assert updates.check_for_update("1.1.0", url=github.url) is None


@pytest.mark.parametrize("payload", [
    release("v9.0.0", prerelease=True),
    release("v9.0.0", draft=True),
    release("v9.0.0", html_url="https://evil.example.com/download.exe"),
    release("latest"),
    ["not", "an", "object"],
])
def test_suspicious_or_unfinished_releases_are_ignored(github, payload):
    github.response = (200, payload)
    assert updates.check_for_update("1.1.0", url=github.url) is None


@pytest.mark.parametrize("response", [(403, {"message": "API rate limit exceeded"}), (500, {}), (200, b"<html>")])
def test_errors(github, response):
    github.response = response
    with pytest.raises(updates.UpdateCheckError):
        updates.check_for_update("1.1.0", url=github.url)


def test_offline():
    with pytest.raises(updates.UpdateCheckError, match="internet"):
        updates.check_for_update("1.1.0", url="http://127.0.0.1:9/releases/latest", timeout=(1, 1))


def test_default_address_uses_repository_id():
    # The permanent numeric id keeps working if the repository is renamed.
    assert updates.LATEST_RELEASE_API == f"https://api.github.com/repositories/{updates.REPOSITORY_ID}/releases/latest"
