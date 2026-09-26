"""Translating image pages: manga, comics, webtoons, scanned books.

A page is rendered, sent to the vision model (tall webtoon strips are split
into overlapping tiles), and the returned text regions are applied to the PDF
page: the original lettering is covered (see cleanup.py) and the translation
is written, as real selectable text, into the same bubble or box.
"""

from __future__ import annotations

import io
import math

import pymupdf
from PIL import Image

from app.core.jobs import JobContext
from app.services.translation.cleanup import plan_cleanup
from app.services.translation.languages import Language
from app.services.translation.models import Region
from app.services.translation.provider import TranslationProvider
from app.services.translation.text_pages import insert_text, text_css

MAX_SIDE_PX = 2000  # long side of a normal page sent to the AI
STRIP_WIDTH_PX = 1400  # width used for tall webtoon strips
STRIP_RATIO = 2.2  # height/width above which a page is treated as a strip
TILE_RATIO = 1.6  # tile height as a multiple of the width
TILE_OVERLAP = 0.15
MAX_PIXELS = 60_000_000


def render(page) -> tuple[Image.Image, float]:
    """The page as an RGB image and the zoom factor (pixels per PDF point)."""
    width, height = page.rect.width, page.rect.height
    if height / max(width, 1) > STRIP_RATIO:
        zoom = STRIP_WIDTH_PX / width
    else:
        zoom = MAX_SIDE_PX / max(width, height)
    zoom = min(max(zoom, 0.5), 4.0)
    if width * height * zoom * zoom > MAX_PIXELS:
        zoom = math.sqrt(MAX_PIXELS / (width * height))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), zoom


def tiles(size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """Vertical tiles (y0, y1, core0, core1) for tall images; a region belongs
    to the tile whose core contains its centre, so overlaps are not doubled."""
    width, height = size
    tile_height = int(width * TILE_RATIO)
    if height <= tile_height * 1.25:
        return [(0, height, 0, height)]
    step = tile_height - int(tile_height * TILE_OVERLAP)
    spans = []
    start = 0
    while start + tile_height < height:
        spans.append((start, start + tile_height))
        start += step
    spans.append((max(0, height - tile_height), height))  # the last tile is full height, ending at the bottom
    # Cores meet in the middle of each overlap, so together they cover the strip exactly once.
    bounds = [0] + [(a[1] + b[0]) // 2 for a, b in zip(spans, spans[1:], strict=False)] + [height]
    return [(y0, y1, bounds[i], bounds[i + 1]) for i, (y0, y1) in enumerate(spans)]


def encode_jpeg(image: Image.Image, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def analyze_page(page, provider: TranslationProvider, target: Language, source: Language | None, doc_type: str,
                 include_sfx: bool, ctx: JobContext) -> list[Region]:
    image, _ = render(page)
    width, height = image.size
    regions: list[Region] = []
    for y0, y1, core0, core1 in tiles(image.size):
        ctx.check_cancelled()
        tile = image.crop((0, y0, width, y1))
        found = provider.analyze_image(encode_jpeg(tile), "image/jpeg", target, source, doc_type, include_sfx, ctx)
        for item in found:
            bx0, by0, bx1, by1 = item["box"]
            top, bottom = y0 + by0 * (y1 - y0), y0 + by1 * (y1 - y0)
            if not core0 <= (top + bottom) / 2 < core1:
                continue  # belongs to the neighbouring tile
            regions.append(Region((bx0, top / height, bx1, bottom / height), item["kind"], item["original"],
                                  item["translation"]))
    return regions


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=False)
    return buffer.getvalue()


def _hex(color: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color[:3])


def font_size_hint(region: Region, box_w_pt: float, box_h_pt: float) -> float:
    """Starting font size from how densely the original text filled its box
    (the text is shrunk further if the translation is longer)."""
    letters = [ch for ch in region.original if not ch.isspace()]
    if not letters:
        return max(7.0, min(24.0, box_h_pt * 0.6))
    wide = sum(1 for ch in letters if ord(ch) > 0x2E80) / len(letters)
    per_char = box_w_pt * box_h_pt / len(letters)
    size = math.sqrt(per_char / (1.0 if wide > 0.5 else 0.55)) * 0.85
    return max(6.0, min(36.0, size))


def flatten_rotated_page(doc, index: int):
    """Replace a rotated page by an upright image of it (keeps coordinates simple)."""
    page = doc[index]
    image, zoom = render(page)
    width, height = image.width / zoom, image.height / zoom
    doc.delete_page(index)
    new_page = doc.new_page(pno=index, width=width, height=height)
    new_page.insert_image(new_page.rect, stream=encode_jpeg(image, 92))
    return new_page


def apply_regions(page, regions: list[Region], language: Language) -> int:
    """Cover the original text of each region and write its translation."""
    if not regions:
        return 0
    image, zoom = render(page)
    width, height = image.size
    applied = 0
    for region in regions:
        box = (region.box[0] * width, region.box[1] * height, region.box[2] * width, region.box[3] * height)
        plan = plan_cleanup(image, box, region.kind)
        if plan.patch is not None and plan.patch_box is not None:
            rect = pymupdf.Rect(*(v / zoom for v in plan.patch_box))
            page.insert_image(rect, stream=_png(plan.patch), overlay=True)
            image.paste(plan.patch, plan.patch_box[:2], plan.patch)  # later regions see the cleaned page
        text_rect = pymupdf.Rect(*(v / zoom for v in plan.text_box))
        size = font_size_hint(region, (box[2] - box[0]) / zoom, (box[3] - box[1]) / zoom)
        if plan.label:
            size = min(size, 14.0)
            page.draw_rect(text_rect, color=None, fill=(1, 1, 1), fill_opacity=0.82)
        css = text_css(size, _hex(plan.foreground), bold=region.kind in ("dialogue", "thought", "sfx"), serif=False,
                       language=language, center=True)
        insert_text(page, text_rect, region.translation, css, center_vertically=True)
        applied += 1
    return applied
