"""Shared helpers for the translation tests: a fake AI provider and test documents."""

from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from app.core.jobs import JobContext
from app.services.translation.provider import ContentBlockedError, ModelInfo, TranslationProvider

# Target-language markers the fake "translation" starts with.
MARKERS = {"en": "Translated", "fr": "Traduit", "hi": "अनुवादित", "te": "అనువాదం", "ja": "翻訳済み", "ko": "번역됨",
           "zh-Hans": "已翻译", "es": "Traducido", "de": "Übersetzt"}


class FakeProvider(TranslationProvider):
    """Deterministic stand-in for the AI service."""

    name = "Fake AI"

    def __init__(self, fail_pages=(), fatal_on_call: int | None = None, fatal_error=None, regions=None,
                 cancel_on_call: int | None = None):
        super().__init__()
        self.fail_pages = set(fail_pages)
        self.fatal_on_call = fatal_on_call
        self.fatal_error = fatal_error
        self.cancel_on_call = cancel_on_call
        self.regions = regions or []  # list of dicts returned for every image
        self.text_calls: list[list[str]] = []
        self.image_calls: list[tuple[int, int]] = []  # image sizes
        self.calls = 0

    def _count(self, ctx: JobContext) -> None:
        self.calls += 1
        self.usage.requests += 1
        if self.cancel_on_call is not None and self.calls == self.cancel_on_call:
            ctx.cancel()
            ctx.check_cancelled()
        if self.fatal_on_call is not None and self.calls == self.fatal_on_call:
            raise self.fatal_error

    def list_models(self, ctx=None):
        return [ModelInfo("fake-model", "Fake model")]

    def translate_segments(self, segments, target, source, context, ctx):
        self._count(ctx)
        self.text_calls.append([s["id"] for s in segments])
        for segment in segments:
            page = int(segment["id"][1:].split("b")[0]) - 1
            if page in self.fail_pages:
                raise ContentBlockedError("The fake AI refused this page.")
        marker = MARKERS.get(target.code, target.name)
        return {s["id"]: f"{marker} {s['id']}" for s in segments}

    def analyze_image(self, image, mime_type, target, source, doc_type, include_sfx, ctx):
        self._count(ctx)
        from io import BytesIO

        with Image.open(BytesIO(image)) as img:
            self.image_calls.append(img.size)
        return [dict(region) for region in self.regions]


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_text_pdf(path: Path, pages: int = 3, with_image: bool = True) -> Path:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 80), f"Chapter {number}", fontsize=20)
        page.insert_textbox(pymupdf.Rect(72, 110, 523, 300),
                            f"This is the first paragraph of page {number}. It explains the story in plain words "
                            "so that the translator has real sentences to work with.", fontsize=12)
        page.insert_textbox(pymupdf.Rect(72, 320, 523, 480),
                            f"The second paragraph of page {number} continues the story with more detail.", fontsize=12)
        if with_image and number == 1:
            picture = Image.new("RGB", (200, 120), (30, 120, 200))
            import io

            buffer = io.BytesIO()
            picture.save(buffer, "PNG")
            page.insert_image(pymupdf.Rect(72, 520, 272, 640), stream=buffer.getvalue())
        page.insert_text((290, 800), str(number), fontsize=10)  # page number: no letters, left alone
    doc.save(str(path))
    doc.close()
    return path


def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_manga_page(size=(1000, 1500)):
    """A synthetic comic page: patterned artwork, two speech bubbles, one caption box.
    Returns (image, bubbles, text_boxes) - boxes in pixels."""
    width, height = size
    image = Image.new("RGB", size, (150, 160, 170))
    draw = ImageDraw.Draw(image)
    for offset in range(-height, width, 24):  # diagonal "artwork"
        draw.line([(offset, 0), (offset + height, height)], fill=(90, 100, 120), width=6)
    bubbles = [(80, 80, 520, 420), (520, 700, 940, 1020)]
    caption = (80, 1180, 700, 1320)
    font = _font(40)
    texts = []
    for (x0, y0, x1, y1), words in zip(bubbles, (["HELLO", "THERE!"], ["WHERE", "ARE", "YOU?"]), strict=True):
        draw.ellipse((x0, y0, x1, y1), fill=(255, 255, 255), outline=(0, 0, 0), width=5)
        cy = (y0 + y1) // 2 - 25 * len(words)
        boxes = []
        for number, word in enumerate(words):
            box = draw.textbbox(((x0 + x1) // 2, cy + number * 50), word, font=font, anchor="mt")
            draw.text(((x0 + x1) // 2, cy + number * 50), word, fill=(0, 0, 0), font=font, anchor="mt")
            boxes.append(box)
        texts.append((min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                      max(b[3] for b in boxes)))
    draw.rectangle(caption, fill=(255, 255, 240), outline=(0, 0, 0), width=4)
    box = draw.textbbox((caption[0] + 30, caption[1] + 45), "MEANWHILE...", font=font)
    draw.text((caption[0] + 30, caption[1] + 45), "MEANWHILE...", fill=(0, 0, 0), font=font)
    texts.append(box)
    return image, bubbles + [caption], texts


def image_pdf(path: Path, image: Image.Image) -> Path:
    import io

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=image.width * 0.72, height=image.height * 0.72)
    page.insert_image(page.rect, stream=buffer.getvalue())
    doc.save(str(path))
    doc.close()
    return path


def regions_for(texts, size, translations) -> list[dict]:
    width, height = size
    kinds = ["dialogue", "dialogue", "caption"]
    return [{"box": (b[0] / width, b[1] / height, b[2] / width, b[3] / height), "kind": kinds[i % 3],
             "original": "ORIGINAL", "translation": translations[i]} for i, b in enumerate(texts)]
