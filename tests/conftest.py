"""Shared test fixtures. Everything is generated locally - no live websites."""

from __future__ import annotations

import http.server
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    """Keep settings/logs/caches of the test run away from the real profile."""
    data = tmp_path_factory.mktemp("kmt-data")
    monkeypatch.setenv("KMT_DATA_DIR", str(data))
    return data


# ----------------------------------------------------------------------------
# Images and PDFs
# ----------------------------------------------------------------------------
def make_image(path: Path, size=(64, 48), color=(200, 30, 30), mode="RGB", orientation: int | None = None) -> Path:
    from PIL import Image

    img = Image.new(mode, size, color if mode != "L" else 128)
    # Mark the top-left corner so orientation changes can be detected.
    if mode in ("RGB", "RGBA"):
        marker = (0, 0, 255, 255) if mode == "RGBA" else (0, 0, 255)
        for x in range(8):
            for y in range(8):
                img.putpixel((x, y), marker)
    params = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        params["exif"] = exif.tobytes()
    img.save(path, **params)
    return path


def make_pdf(path: Path, pages: int, label: str = "Page") -> Path:
    import pymupdf

    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=300, height=400)
        page.insert_text((50, 100), f"{label} {number}", fontsize=24)
    doc.save(str(path))
    doc.close()
    return path


def pdf_texts(path: Path) -> list[str]:
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        return [page.get_text().strip() for page in doc]


@pytest.fixture
def image_factory(tmp_path):
    def factory(name="img.png", **kwargs):
        return make_image(tmp_path / name, **kwargs)

    return factory


@pytest.fixture
def pdf_factory(tmp_path):
    def factory(name="doc.pdf", pages=3, label="Page"):
        return make_pdf(tmp_path / name, pages, label)

    return factory


# ----------------------------------------------------------------------------
# FFmpeg test media
# ----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def media_tools():
    from app.services.tools import MediaTools, find_tool

    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not (ffmpeg.found and ffprobe.found):
        pytest.skip("FFmpeg/FFprobe not available")
    return MediaTools(ffmpeg.path, ffprobe.path)


def _ffmpeg(tools, *args: str) -> None:
    subprocess.run([str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def sample_video(media_tools, media_dir) -> Path:
    """3 s, 320x240, 25 fps H.264 + AAC."""
    out = media_dir / "sample.mp4"
    _ffmpeg(media_tools, "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac", "-shortest", str(out))
    return out


@pytest.fixture(scope="session")
def sample_video_2(media_tools, media_dir) -> Path:
    """2 s, same parameters as sample_video (joinable without re-encoding)."""
    out = media_dir / "sample2.mp4"
    _ffmpeg(media_tools, "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac", "-shortest", str(out))
    return out


@pytest.fixture(scope="session")
def sample_video_other(media_tools, media_dir) -> Path:
    """2 s, 640x360 @ 30 fps, no audio - forces re-encoding when joined."""
    out = media_dir / "other.mkv"
    _ffmpeg(media_tools, "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out))
    return out


@pytest.fixture(scope="session")
def sample_audio(media_tools, media_dir) -> Path:
    """4 s MP3 at 128 kbps with tags."""
    out = media_dir / "tone.mp3"
    _ffmpeg(media_tools, "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4",
            "-c:a", "libmp3lame", "-b:a", "128k", "-metadata", "title=Test Tone", "-metadata", "artist=Tester",
            str(out))
    return out


@pytest.fixture(scope="session")
def sample_audio_2(media_tools, media_dir) -> Path:
    out = media_dir / "tone2.wav"
    _ffmpeg(media_tools, "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2", str(out))
    return out


# ----------------------------------------------------------------------------
# Local HTTP server
# ----------------------------------------------------------------------------
class _Handler(http.server.SimpleHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):  # noqa: N802 - http.server API
        route = self.routes.get(self.path)
        if route is None:
            return super().do_GET()
        status, headers, body = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        if self.path in self.routes:
            status, headers, body = self.routes[self.path]
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            return None
        return super().do_HEAD()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def http_server(tmp_path):
    """Serves ``tmp_path/www`` plus custom routes; yields (base_url, www_dir, routes)."""
    www = tmp_path / "www"
    www.mkdir()
    routes: dict = {}
    handler = type("Handler", (_Handler,), {"routes": routes})

    def factory(*args, **kwargs):
        return handler(*args, directory=str(www), **kwargs)

    server = _Server(("127.0.0.1", 0), factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    # Make sure no proxy settings intercept localhost requests.
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    try:
        yield base, www, routes
    finally:
        server.shutdown()
        server.server_close()
