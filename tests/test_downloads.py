"""Download tests against a local HTTP server - no live websites."""

import shutil
import subprocess

import pytest

from app.core.errors import DownloadError, InvalidInputError, JobCancelled
from app.core.jobs import JobContext
from app.models.remote import FormatKind
from app.services.downloads import http
from app.services.downloads.plans import plan_audio_download, plan_video_download
from app.services.downloads.ytdlp import DownloadTarget, EmbeddedYtDlp, ExternalYtDlp
from app.services.ffmpeg.probe import probe
from tests.conftest import make_pdf


# ----------------------------------------------------------------------------
# File names
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("header, expected", [
    ('attachment; filename="report 2024.pdf"', "report 2024.pdf"),
    ("attachment; filename=plain.pdf", "plain.pdf"),
    ("attachment; filename*=UTF-8''%E2%82%AC%20rates.pdf", "€ rates.pdf"),
    ('attachment; filename="fallback.pdf"; filename*=UTF-8\'\'real%20name.pdf', "real name.pdf"),
    ('attachment; filename="..\\..\\evil.pdf"', "evil.pdf"),
    ("inline", None),
    (None, None),
])
def test_content_disposition(header, expected):
    assert http.filename_from_content_disposition(header) == expected


@pytest.mark.parametrize("url, header, expected", [
    ("https://x.org/files/Annual%20Report.pdf", None, "Annual Report.pdf"),
    ("https://x.org/download.php?id=7", None, "download.pdf"),
    ("https://x.org/download.php?id=7", 'attachment; filename="Invoice 7.pdf"', "Invoice 7.pdf"),
    ("https://x.org/", None, "document.pdf"),
    ("https://x.org/a:b|c.pdf", None, "a_b_c.pdf"),
    ("https://x.org/paper.v2", None, "paper.v2.pdf"),
])
def test_suggest_pdf_filename(url, header, expected):
    assert http.suggest_pdf_filename(url, header) == expected


# ----------------------------------------------------------------------------
# Direct PDF downloads
# ----------------------------------------------------------------------------
@pytest.fixture
def served_pdf(http_server, tmp_path):
    base, www, routes = http_server
    make_pdf(www / "sample.pdf", 3)
    return base, www, routes


def test_download_pdf(served_pdf, tmp_path):
    base, www, _ = served_pdf
    out = tmp_path / "dl" / "saved.pdf"
    progress = []
    ctx = JobContext(on_progress=lambda f, d: progress.append((f, d)), min_interval=0)
    result = http.download_pdf(f"{base}/sample.pdf", out, ctx)
    assert out.read_bytes() == (www / "sample.pdf").read_bytes()
    assert "3 page(s)" in result.message
    assert any(f == 1.0 for f, _ in progress)
    assert not [p for p in out.parent.iterdir() if "kmt-partial" in p.name]


def test_redirect_and_inspect(served_pdf):
    base, www, routes = served_pdf
    routes["/go"] = (302, {"Location": "/sample.pdf"}, b"")
    routes["/named"] = (200, {"Content-Type": "application/pdf",
                              "Content-Disposition": 'attachment; filename="Quarterly Report.pdf"'},
                        (www / "sample.pdf").read_bytes())
    info = http.inspect_link(f"{base}/go")
    assert info.url.endswith("/sample.pdf") and info.is_pdf and info.filename == "sample.pdf" and info.size
    assert http.inspect_link(f"{base}/named").filename == "Quarterly Report.pdf"


def test_html_instead_of_pdf(served_pdf, tmp_path):
    base, _, routes = served_pdf
    routes["/page"] = (200, {"Content-Type": "text/html"}, b"<html><body>" + b"x" * 3000 + b"</body></html>")
    out = tmp_path / "page.pdf"
    with pytest.raises(DownloadError, match="web page"):
        http.download_pdf(f"{base}/page", out, JobContext())
    assert not out.exists() and not list(tmp_path.glob("*kmt-partial*"))


@pytest.mark.parametrize("status, text", [(404, "404"), (403, "forbidden"), (500, "internal problem")])
def test_http_errors(served_pdf, tmp_path, status, text):
    base, _, routes = served_pdf
    routes["/err"] = (status, {"Content-Type": "text/plain"}, b"error")
    with pytest.raises(DownloadError, match=text):
        http.download_pdf(f"{base}/err", tmp_path / "x.pdf", JobContext())


def test_truncated_download(served_pdf, tmp_path):
    base, www, routes = served_pdf
    body = (www / "sample.pdf").read_bytes()
    routes["/short"] = (200, {"Content-Type": "application/pdf", "Content-Length": str(len(body) + 5000)}, body)
    out = tmp_path / "short.pdf"
    with pytest.raises(DownloadError):
        http.download_pdf(f"{base}/short", out, JobContext())
    assert not out.exists()


def test_damaged_pdf(served_pdf, tmp_path):
    base, _, routes = served_pdf
    routes["/broken"] = (200, {"Content-Type": "application/pdf"}, b"%PDF-1.7\n" + b"\x00garbage" * 300)
    with pytest.raises(DownloadError, match="damaged"):
        http.download_pdf(f"{base}/broken", tmp_path / "b.pdf", JobContext())


def test_invalid_url_and_refused_connection(tmp_path):
    with pytest.raises(InvalidInputError):
        http.download_pdf("ftp://example.com/x.pdf", tmp_path / "x.pdf", JobContext())
    with pytest.raises(DownloadError, match="connect"):
        http.download_pdf("http://127.0.0.1:9/nothing.pdf", tmp_path / "x.pdf", JobContext())


def test_cancel_before_data(served_pdf, tmp_path):
    base, _, _ = served_pdf
    ctx = JobContext()
    ctx.cancel()
    with pytest.raises(JobCancelled):
        http.download_pdf(f"{base}/sample.pdf", tmp_path / "c.pdf", ctx)
    assert not (tmp_path / "c.pdf").exists()


# ----------------------------------------------------------------------------
# yt-dlp (generic extractor against the local server)
# ----------------------------------------------------------------------------
@pytest.fixture
def served_video(http_server, sample_video):
    base, www, _ = http_server
    shutil.copy(sample_video, www / "clip.mp4")
    return f"{base}/clip.mp4"


@pytest.fixture
def served_dash(http_server, media_tools):
    """A DASH stream with separate video-only and audio-only representations."""
    base, www, _ = http_server
    folder = www / "dash"
    folder.mkdir()
    # FFmpeg's DASH muxer only understands "/" in the manifest path and would
    # write the segments into the current directory, so run it inside `folder`.
    subprocess.run([
        str(media_tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac",
        "-f", "dash", "-seg_duration", "1", "-use_template", "1", "-use_timeline", "1",
        "manifest.mpd",
    ], check=True, cwd=folder)
    return f"{base}/dash/manifest.mpd"


def _embedded(media_tools):
    return EmbeddedYtDlp(media_tools.ffmpeg, {}, embed_metadata=True)


@pytest.mark.ffmpeg
def test_ytdlp_direct_file(media_tools, served_video, tmp_path):
    backend = _embedded(media_tools)
    media = backend.analyze(served_video, JobContext())
    assert len(media.formats) == 1 and media.formats[0].kind is FormatKind.COMBINED
    plan = plan_video_download(media, media.formats[0], "auto", True)
    progress = []
    ctx = JobContext(on_progress=lambda f, d: progress.append(f), min_interval=0)
    out = backend.download(served_video, plan, DownloadTarget(tmp_path, "My Clip"), ctx)
    assert out == tmp_path / "My Clip.mp4" and out.exists()
    assert probe(media_tools.ffprobe, out).has_video
    assert 1.0 in progress
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".kmt-download-")]


@pytest.mark.ffmpeg
def test_ytdlp_dash_merges_video_and_audio(media_tools, served_dash, tmp_path):
    backend = _embedded(media_tools)
    media = backend.analyze(served_dash, JobContext())
    kinds = {f.kind for f in media.formats}
    assert FormatKind.VIDEO_ONLY in kinds and FormatKind.AUDIO_ONLY in kinds
    video_only = next(f for f in media.formats if f.kind is FormatKind.VIDEO_ONLY)
    plan = plan_video_download(media, video_only, "auto", True)
    assert "+" in plan.format_selector and plan.final_ext == "mp4"
    statuses = []
    ctx = JobContext(on_status=statuses.append, min_interval=0)
    out = backend.download(served_dash, plan, DownloadTarget(tmp_path, "merged"), ctx)
    info = probe(media_tools.ffprobe, out)
    assert out.suffix == ".mp4" and info.has_video and info.has_audio
    assert any("Combining video and audio" in s for s in statuses)


@pytest.mark.ffmpeg
def test_ytdlp_audio_conversion(media_tools, served_dash, tmp_path):
    backend = _embedded(media_tools)
    media = backend.analyze(served_dash, JobContext())
    plan = plan_audio_download(media, media.audio_formats[0], "mp3", 128)
    out = backend.download(served_dash, plan, DownloadTarget(tmp_path, "song"), JobContext())
    info = probe(media_tools.ffprobe, out)
    assert out.name == "song.mp3" and info.primary_audio.codec == "mp3" and not info.has_video


@pytest.mark.ffmpeg
def test_ytdlp_existing_file_is_kept(media_tools, served_video, tmp_path):
    (tmp_path / "clip.mp4").write_text("keep")
    backend = _embedded(media_tools)
    media = backend.analyze(served_video, JobContext())
    plan = plan_video_download(media, media.formats[0], "auto", True)
    out = backend.download(served_video, plan, DownloadTarget(tmp_path, "clip"), JobContext())
    assert out.name == "clip (1).mp4" and (tmp_path / "clip.mp4").read_text() == "keep"


@pytest.mark.ffmpeg
def test_ytdlp_cancel(media_tools, served_dash, tmp_path):
    backend = _embedded(media_tools)
    media = backend.analyze(served_dash, JobContext())
    plan = plan_video_download(media, None, "auto", True)
    ctx = JobContext()
    ctx.set_progress = lambda *a, **k: ctx.cancel()  # cancel as soon as progress is reported
    out_dir = tmp_path / "out"
    with pytest.raises(JobCancelled):
        backend.download(served_dash, plan, DownloadTarget(out_dir, "cancelled"), ctx)
    assert list(out_dir.iterdir()) == []  # temporary download folder removed


def test_ytdlp_rejects_bad_urls(media_tools):
    backend = EmbeddedYtDlp(None, {})
    with pytest.raises(InvalidInputError):
        backend.analyze("file:///C:/Windows/win.ini", JobContext())
    with pytest.raises(InvalidInputError):
        backend.analyze("not a link", JobContext())


def test_ytdlp_unsupported_site(http_server):
    base, www, routes = http_server
    routes["/plain"] = (200, {"Content-Type": "text/html"}, b"<html><body>No media here</body></html>")
    with pytest.raises(DownloadError):
        EmbeddedYtDlp(None, {}).analyze(f"{base}/plain", JobContext())


@pytest.mark.ffmpeg
def test_external_ytdlp_backend(media_tools, served_video, tmp_path):
    exe = shutil.which("yt-dlp")
    if not exe:
        pytest.skip("yt-dlp.exe not installed")
    from pathlib import Path

    backend = ExternalYtDlp(Path(exe), media_tools.ffmpeg, {}, embed_metadata=False)
    assert backend.version()
    media = backend.analyze(served_video, JobContext())
    plan = plan_video_download(media, media.formats[0], "auto", True)
    progress = []
    ctx = JobContext(on_progress=lambda f, d: progress.append(f), min_interval=0)
    out = backend.download(served_video, plan, DownloadTarget(tmp_path, "external"), ctx)
    assert out.name == "external.mp4" and probe(media_tools.ffprobe, out).has_video
