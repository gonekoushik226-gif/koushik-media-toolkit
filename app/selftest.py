"""Built-in self-test: ``MediaToolkit.exe --self-test --self-test-output report.json``.

Used by build.py to verify that a *packaged* build really works (Qt plugins,
bundled FFmpeg, PDF/image libraries, yt-dlp). Everything runs offline in a
temporary folder; nothing in the user's profile is touched. The exit code is
0 when every check passed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from app import APP_NAME, __version__

NETWORK_TEST_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # Blender Foundation, public


def run_self_test(output_file: str = "", network: bool = False, download_url: str = "") -> int:
    """``network`` adds an HTTPS check and a metadata-only yt-dlp analysis of a
    public video. ``download_url`` analyses and downloads that link (use a
    local test server) to exercise the full download + merge path."""
    work = Path(tempfile.mkdtemp(prefix="mt-selftest-"))
    os.environ["MEDIA_TOOLKIT_DATA_DIR"] = str(work / "data")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.config import paths

    report: dict = {"app": APP_NAME, "version": __version__, "frozen": paths.is_frozen(),
                    "python": sys.version.split()[0], "checks": {}}
    passed = True

    def check(name: str, func) -> object:
        nonlocal passed
        started = time.monotonic()
        try:
            value = func()
            report["checks"][name] = {"ok": True, "info": value, "seconds": round(time.monotonic() - started, 2)}
            return value
        except BaseException as exc:  # noqa: BLE001 - report every failure
            passed = False
            report["checks"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                      "traceback": traceback.format_exc()[-3000:]}
            return None

    def libraries() -> dict:
        import certifi
        import PIL
        import pymupdf
        import pypdf
        import requests
        import yt_dlp.version
        import yt_dlp_ejs  # noqa: F401 - YouTube challenge scripts must be bundled
        from PySide6 import QtCore

        return {"Pillow": PIL.__version__, "pypdf": pypdf.__version__, "PyMuPDF": pymupdf.VersionBind,
                "yt-dlp": yt_dlp.version.__version__, "requests": requests.__version__, "Qt": QtCore.qVersion(),
                "certifi_bundle_exists": Path(certifi.where()).is_file()}

    def tools() -> dict:
        from app.services.tools import find_tool, tool_version

        result = {}
        for name in ("ffmpeg", "ffprobe"):
            info = find_tool(name)
            if not info.found:
                raise RuntimeError(f"{name} not found")
            result[name] = {"path": str(info.path), "source": info.source, "version": tool_version(info.path)[:60]}
        return result

    def media() -> dict:
        import subprocess

        from app.core.jobs import JobContext
        from app.services.tools import MediaTools, find_tool
        from app.services.video import VideoService
        from app.utils.system import hidden_subprocess_kwargs

        ffmpeg, ffprobe = find_tool("ffmpeg").path, find_tool("ffprobe").path
        source = work / "test.mp4"
        subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=160x120:rate=10:duration=2", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", str(source)], check=True, capture_output=True, **hidden_subprocess_kwargs())
        service = VideoService(MediaTools(ffmpeg, ffprobe))
        trimmed = work / "trimmed.mp4"
        service.trim(source, trimmed, 0.5, 1.5, True, JobContext())
        info = service.processor.probe(trimmed)
        if not (info.has_video and info.has_audio):
            raise RuntimeError("trimmed file is missing streams")
        return {"trimmed_duration": info.duration}

    def pdf_and_images() -> dict:
        from PIL import Image

        from app.core.jobs import JobContext
        from app.services import pdf

        images = []
        for index, color in enumerate(("red", "green", "blue")):
            path = work / f"img{index}.jpg"
            Image.new("RGB", (120, 80), color).save(path)
            images.append(pdf.ImageSource(path))
        combined = work / "combined.pdf"
        pdf.images_to_pdf(images, combined, "a4", JobContext())
        parts = pdf.split_pdf(combined, work / "parts", "part", pdf.compute_equal_parts(3, 2), JobContext())
        merged = work / "merged.pdf"
        pdf.merge_pdfs(parts.outputs, merged, JobContext())
        rendered = pdf.pdf_to_images(merged, work / "pages", "page", "png", 50, JobContext())
        return {"pages": pdf.count_pages(merged), "rendered": len(rendered.outputs)}

    def gui() -> dict:
        from PySide6.QtWidgets import QApplication

        from app.config.settings import Settings, SettingsStore
        from app.services.tools import ToolLocator
        from app.ui.context import AppContext
        from app.ui.main_window import MainWindow
        from app.ui.modules import build_registry
        from app.ui.theme import apply_theme

        app = QApplication.instance() or QApplication([sys.argv[0]])
        apply_theme(app, "light")
        store = SettingsStore(work / "data" / "settings.json")
        from app.config.credentials import MemorySecretStore
        from app.services.translation.keys import ApiKeyManager

        ctx = AppContext(settings=Settings(), store=store, tools=None,  # type: ignore[arg-type]
                         api_keys=ApiKeyManager(MemorySecretStore()))  # never touch the user's saved key
        ctx.tools = ToolLocator(lambda: ctx.settings)
        registry = build_registry()
        window = MainWindow(ctx, registry)
        window.show()
        opened = []
        for spec in registry.specs():
            window.navigate(spec.key)
            app.processEvents()
            opened.append(spec.key)
        window.navigate("home")
        app.processEvents()
        shot = work / "home.png"
        if not window.grab().save(str(shot)):
            raise RuntimeError("could not render the window")
        from PySide6.QtGui import QImageReader

        formats = sorted(bytes(f).decode() for f in QImageReader.supportedImageFormats())
        window.close()
        return {"pages": opened, "image_formats": formats}

    def translation() -> dict:
        """Offline: the translation pipeline with a stand-in provider (no key, no
        network), complex-script fonts, and the Windows credential store."""
        import uuid

        import pymupdf

        from app.config.credentials import default_store
        from app.core.jobs import JobContext
        from app.services.translation import job as tjob
        from app.services.translation.languages import get_language
        from app.services.translation.provider import ModelInfo, TranslationProvider

        class Offline(TranslationProvider):
            name = "offline self-test"

            def list_models(self, ctx=None):
                return [ModelInfo("offline", "offline")]

            def translate_segments(self, segments, target, source, context, ctx):
                return {s["id"]: "తెలుగు अनुवाद 日本語 " + s["id"] for s in segments}

            def analyze_image(self, image, mime_type, target, source, doc_type, include_sfx, ctx):
                return [{"box": (0.1, 0.1, 0.6, 0.3), "kind": "dialogue", "original": "x", "translation": "Hello"}]

        source = work / "translate-source.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 300), "A paragraph of plain English text for the offline "
                            "translation self-test, long enough to count as a text page.", fontsize=12)
        picture = doc.new_page(width=300, height=400)
        picture.draw_rect(picture.rect, fill=(0.6, 0.7, 0.8))
        doc.save(str(source))
        doc.close()
        output = work / "translate-output.pdf"
        request = tjob.TranslationRequest(source, output, get_language("te"), model="offline")
        tjob.translate_document(request, Offline(), JobContext(), cache_dir=work / "translate-cache")
        with pymupdf.open(str(output)) as result:
            text = result[0].get_text()
            fonts = sorted({f[3] for f in result[0].get_fonts()})
            # (Some shaped Indic ligatures do not extract back to the same characters, so Telugu is
            # checked through its embedded font; Devanagari and CJK extract cleanly.)
            if "अनुवाद" not in text or "日本語" not in text or "Hello" not in result[1].get_text():
                raise RuntimeError("translated text is missing from the output")
            if not any("Telugu" in font for font in fonts):
                raise RuntimeError(f"no Telugu font was embedded (fonts: {fonts})")
        store = default_store()
        name = f"self-test {uuid.uuid4().hex}"
        store.set(name, "not-a-real-key")
        try:
            if store.get(name) != "not-a-real-key":
                raise RuntimeError("credential store round trip failed")
        finally:
            store.delete(name)
        return {"fonts": fonts, "credential_store": store.description}

    def internet() -> dict:
        from app.services.diagnostics import OK, _internet_check

        result = _internet_check()
        if result.status != OK:
            raise RuntimeError(f"{result.summary}: {result.details}")
        return {"summary": result.summary, "details": result.details}

    def analyze_online() -> dict:
        from app.config.settings import Settings
        from app.core.jobs import JobContext
        from app.services.downloads.ytdlp import get_backend
        from app.services.tools import ToolLocator

        backend = get_backend(ToolLocator(lambda: Settings()))
        media = backend.analyze(NETWORK_TEST_URL, JobContext())
        if len(media.formats) < 5:
            raise RuntimeError(f"only {len(media.formats)} formats found")
        return {"title": media.title, "formats": len(media.formats), "js_runtimes": sorted(backend.js_runtimes),
                "heights": sorted({f.height for f in media.formats if f.height}, reverse=True)}

    def download() -> dict:
        from app.config.settings import Settings
        from app.core.jobs import JobContext
        from app.services.downloads.plans import plan_video_download
        from app.services.downloads.ytdlp import DownloadTarget, get_backend
        from app.services.ffmpeg.probe import probe
        from app.services.tools import ToolLocator, find_tool

        backend = get_backend(ToolLocator(lambda: Settings()))
        media = backend.analyze(download_url, JobContext())
        plan = plan_video_download(media, None, "auto", True)
        path = backend.download(download_url, plan, DownloadTarget(work / "downloads", "selftest"), JobContext())
        info = probe(find_tool("ffprobe").path, path)
        if not info.has_video:
            raise RuntimeError("downloaded file has no video")
        return {"plan": plan.summary, "file": path.name, "size": path.stat().st_size, "has_audio": info.has_audio}

    check("libraries", libraries)
    check("tools", tools)
    check("media", media)
    check("pdf_and_images", pdf_and_images)
    check("translation", translation)
    check("gui", gui)
    if network:
        check("internet_https", internet)
        check("ytdlp_online_analysis", analyze_online)
    if download_url:
        check("ytdlp_download", download)
    report["passed"] = passed
    text = json.dumps(report, indent=2, default=str)
    if output_file:
        Path(output_file).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if passed else 1
