"""Translating PDF pages that contain real (selectable) text.

Each text block keeps its place on the page: the original text is removed
with a redaction that leaves images and drawings untouched, and the
translation is written into the same rectangle (shrunk to fit if longer).
"""

from __future__ import annotations

import html
import re
import unicodedata

import pymupdf

from app.services.translation.languages import Language
from app.services.translation.models import TextSegment

MIN_TEXT_CHARS = 30  # fewer visible characters than this -> treat the page as an image
_LIST_START = re.compile(r"^([•·\-–—*]|\(?\d{1,3}[.)]|[a-zA-Z][.)])\s")


def text_visibility(page) -> tuple[int, int]:
    """(visible, invisible) character counts. Scanned PDFs often carry an
    invisible OCR text layer, which must not be treated as real text."""
    visible = invisible = 0
    try:
        for span in page.get_texttrace():
            count = len(span.get("chars", ()))
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                invisible += count
            else:
                visible += count
    except Exception:  # noqa: BLE001 - very old/odd PDFs: fall back to plain extraction
        visible = len(page.get_text("text").strip())
    return visible, invisible


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def join_lines(lines: list[tuple[str, tuple]]) -> str:
    """Join the lines of one block into flowing text, keeping intentional
    breaks (list items, short lines) and repairing hyphenation."""
    if not lines:
        return ""
    widths = [bbox[2] - bbox[0] for _, bbox in lines]
    widest = max(widths) or 1
    out = lines[0][0].strip()
    for (prev, prev_box), (cur, _) in zip(lines, lines[1:], strict=False):
        current = cur.strip()
        previous = prev.rstrip()
        if not current:
            continue
        short_previous = (prev_box[2] - prev_box[0]) < 0.6 * widest
        if previous.endswith("-") and not previous.endswith(" -") and current[:1].islower():
            out = out[:-1] + current
        elif _LIST_START.match(current) or (short_previous and not previous.endswith(("-", ","))):
            out += "\n" + current
        elif previous and _is_wide(previous[-1]) and _is_wide(current[0]):
            out += current  # CJK text has no spaces between lines
        else:
            out += " " + current
    return out


def has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def extract_segments(page, page_index: int) -> list[TextSegment]:
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT, sort=True)
    segments: list[TextSegment] = []
    for number, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        lines, spans = [], []
        for line in block.get("lines", []):
            line_spans = [s for s in line.get("spans", []) if s.get("text")]
            text = "".join(s["text"] for s in line_spans)
            if text.strip():
                lines.append((text, tuple(line.get("bbox", block["bbox"]))))
                spans.extend(s for s in line_spans if s["text"].strip())
        if not lines or not spans:
            continue
        text = join_lines(lines)
        if not has_letters(text):
            continue  # page numbers, symbols: left as they are
        sizes = sorted(float(s.get("size", 11)) for s in spans)
        first = spans[0]
        font = str(first.get("font", "")).lower()
        flags = int(first.get("flags", 0))
        segments.append(TextSegment(
            id=f"p{page_index + 1}b{number}",
            text=text,
            rect=tuple(round(v, 2) for v in block["bbox"]),
            font_size=round(sizes[len(sizes) // 2], 2),
            color=f"#{int(first.get('color', 0)) & 0xFFFFFF:06x}",
            bold=bool(flags & 16) or "bold" in font or "black" in font,
            serif=bool(flags & 4) or any(k in font for k in ("times", "serif", "roman", "georgia", "garamond", "mincho")),
        ))
    return segments


def text_css(font_size: float, color: str, bold: bool, serif: bool, language: Language, center: bool = False) -> str:
    align = "center" if center else ("right" if language.rtl else "left")
    direction = "direction: rtl;" if language.rtl else ""
    family = "serif" if serif else "sans-serif"
    weight = "bold" if bold else "normal"
    return (f"* {{font-family: {family}; font-size: {font_size:.1f}px; line-height: 1.15; color: {color}; "
            f"font-weight: {weight}; margin: 0; padding: 0;}} body {{text-align: {align}; {direction}}}")


def to_html(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def insert_text(page, rect: pymupdf.Rect, text: str, css: str, center_vertically: bool = False) -> float:
    """Write ``text`` into ``rect`` (shrinking the font if needed so nothing is
    lost). Returns the scale factor that was applied (1.0 = original size)."""
    content = to_html(text)
    if center_vertically:
        scratch = pymupdf.open()
        try:
            probe = scratch.new_page(width=rect.width + 2, height=rect.height + 2)
            spare, _ = probe.insert_htmlbox(pymupdf.Rect(1, 1, 1 + rect.width, 1 + rect.height), content, css=css,
                                            scale_low=0)
        finally:
            scratch.close()
        if spare > 0:
            rect = pymupdf.Rect(rect.x0, rect.y0 + spare / 2, rect.x1, rect.y1)
    _, scale = page.insert_htmlbox(rect, content, css=css, scale_low=0)
    return scale


def apply_translations(page, segments: list[TextSegment], translations: dict[str, str], language: Language) -> int:
    """Replace each translated block on ``page``. Returns how many were replaced."""
    todo = [s for s in segments if translations.get(s.id, "").strip()]
    if not todo:
        return 0
    for segment in todo:
        rect = pymupdf.Rect(segment.rect)
        # Shrink slightly so neighbouring text that only touches the block survives.
        page.add_redact_annot(rect + (0.3, 0.3, -0.3, -0.3), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for segment in todo:
        css = text_css(segment.font_size, segment.color, segment.bold, segment.serif, language)
        insert_text(page, pymupdf.Rect(segment.rect), translations[segment.id].strip(), css)
    return len(todo)
