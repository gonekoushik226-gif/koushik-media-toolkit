"""AI translation pipeline tests (fake AI provider - no network, no API key)."""

from __future__ import annotations

import time

import pymupdf
import pytest
from PIL import Image, ImageChops

from app.core.errors import InvalidInputError, JobCancelled
from app.core.jobs import JobContext
from app.services.translation import cleanup, image_pages, job
from app.services.translation.languages import LANGUAGES, get_language
from app.services.translation.provider import ApiKeyError, QuotaExceededError
from tests.translation_helpers import FakeProvider, image_pdf, make_manga_page, make_text_pdf, regions_for, sha256


def request_for(source, output, lang="en", **kwargs) -> job.TranslationRequest:
    return job.TranslationRequest(source=source, output=output, target=get_language(lang), model="fake", **kwargs)


def page_text(path, index=0) -> str:
    with pymupdf.open(str(path)) as doc:
        return doc[index].get_text("text")


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def test_languages_cover_required_ones():
    names = {lang.name for lang in LANGUAGES}
    for required in ("English", "Hindi", "Telugu", "Spanish", "French", "German", "Japanese", "Korean",
                     "Chinese (Simplified)"):
        assert required in names
    assert get_language("te").native == "తెలుగు" and get_language("Arabic").rtl


@pytest.mark.parametrize("text, expected", [("", None), ("all", None), ("1-3", [0, 1, 2]), ("2, 5-6", [1, 4, 5]),
                                            ("3,3,1", [0, 2])])
def test_parse_page_range(text, expected):
    assert job.parse_page_range(text, 6) == expected


@pytest.mark.parametrize("text", ["0", "7", "2-1", "a-b", "1-99"])
def test_parse_page_range_errors(text):
    with pytest.raises(InvalidInputError):
        job.parse_page_range(text, 6)


def test_default_output_name(tmp_path):
    assert job.default_output_name(tmp_path / "Japanese Book.pdf", get_language("en")) == "Japanese Book - English"
    folder = tmp_path / "Chapter 01"
    folder.mkdir()
    assert job.default_output_name(folder, get_language("hi")) == "Chapter 01 - Hindi"


# ----------------------------------------------------------------------------
# Text PDFs
# ----------------------------------------------------------------------------
def test_text_pdf_is_translated_into_new_pdf(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=3)
    before = sha256(source)
    output = tmp_path / "Book - French.pdf"
    provider = FakeProvider()
    result = job.translate_document(request_for(source, output, "fr"), provider, JobContext(), cache_dir=tmp_path / "c")
    assert sha256(source) == before, "the original must never change"
    assert result.outputs == [output] and "3 of 3" in result.message
    with pymupdf.open(str(output)) as doc, pymupdf.open(str(source)) as original:
        assert doc.page_count == 3
        text = doc[0].get_text("text")
        assert "Traduit p1b" in text and "first paragraph" not in text
        assert len(doc[0].get_images()) == len(original[0].get_images()) == 1  # picture kept
        assert "1" in doc[0].get_text("text").split()  # the page number (no letters) was left alone
        # translated blocks keep their places
        blocks = [b for b in doc[1].get_text("blocks") if "Traduit" in b[4]]
        assert blocks and all(60 <= b[0] <= 80 for b in blocks)
    assert len(provider.text_calls) == 1  # three small pages fit into one request


@pytest.mark.parametrize("code, font_hint", [("hi", "Devanagari"), ("te", "Telugu"), ("ja", "Droid Sans Fallback"),
                                             ("ko", "Droid Sans Fallback"), ("zh-Hans", "Droid Sans Fallback"),
                                             ("es", None), ("de", None)])
def test_target_languages_render(tmp_path, code, font_hint):
    source = make_text_pdf(tmp_path / "in.pdf", pages=1, with_image=False)
    output = tmp_path / "out.pdf"
    job.translate_document(request_for(source, output, code), FakeProvider(), JobContext(), cache_dir=tmp_path / "c")
    with pymupdf.open(str(output)) as doc:
        fonts = " ".join(f[3] for f in doc[0].get_fonts())
        if font_hint:
            assert font_hint in fonts  # a font that really contains the script was embedded
        pix = doc[0].get_pixmap(dpi=40)
        assert pix.width > 0


def test_invisible_ocr_layer_is_treated_as_image(tmp_path):
    source = tmp_path / "scan.pdf"
    image, _, _ = make_manga_page((600, 800))
    image_pdf(source, image)
    with pymupdf.open(str(source)) as doc:
        doc[0].insert_text((50, 50), "hidden ocr text " * 10, render_mode=3)
        doc.save(str(tmp_path / "scan_ocr.pdf"))
    plan = job.analyze_source(tmp_path / "scan_ocr.pdf")
    assert [p.mode for p in plan.pages] == ["image"]


def test_page_range_and_blank_pages(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=4, with_image=False)
    with pymupdf.open(str(source)) as doc:
        doc.new_page()  # blank page 5
        doc.save(str(tmp_path / "Book2.pdf"))
    plan = job.analyze_source(tmp_path / "Book2.pdf")
    assert [p.mode for p in plan.pages] == ["text"] * 4 + ["skip"]
    output = tmp_path / "part.pdf"
    job.translate_document(request_for(tmp_path / "Book2.pdf", output, pages=[1, 2]), FakeProvider(), JobContext(),
                           cache_dir=tmp_path / "c")
    with pymupdf.open(str(output)) as doc:
        assert doc.page_count == 2 and "Chapter 2" not in doc[0].get_text() and "Translated p2b" in doc[0].get_text()


def test_large_document_is_batched(tmp_path):
    source = make_text_pdf(tmp_path / "Big.pdf", pages=120, with_image=False)
    provider = FakeProvider()
    started = time.monotonic()
    job.translate_document(request_for(source, tmp_path / "Big - English.pdf"), provider, JobContext(),
                           cache_dir=tmp_path / "c")
    assert len(provider.text_calls) < 30  # many pages per request, not one request per block
    assert sum(len(c) for c in provider.text_calls) == 120 * 3
    assert time.monotonic() - started < 120


# ----------------------------------------------------------------------------
# Failures, resume, cancel
# ----------------------------------------------------------------------------
def test_failed_page_is_kept_and_retried_next_time(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=3, with_image=False)
    output = tmp_path / "Book - English.pdf"
    cache = tmp_path / "cache"
    first = FakeProvider(fail_pages={1})
    result = job.translate_document(request_for(source, output), first, JobContext(), cache_dir=cache)
    assert "2 of 3" in result.message and result.warnings and "2" in result.warnings[0]
    assert "Chapter 2" in page_text(output, 1)  # untranslated page left as it was
    second = FakeProvider()
    job.translate_document(request_for(source, output), second, JobContext(), cache_dir=cache)
    assert [ids for ids in second.text_calls] == [["p2b0", "p2b1", "p2b2"]]  # only the failed page is sent again
    assert "Translated p2b" in page_text(output, 1)
    assert not any(cache.iterdir())  # everything finished -> progress removed


def test_fatal_error_keeps_progress_and_writes_nothing(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=3, with_image=False)
    output = tmp_path / "out.pdf"
    job.BATCH_CHARS, original_batch = 250, job.BATCH_CHARS  # one page per request
    try:
        provider = FakeProvider(fatal_on_call=3, fatal_error=QuotaExceededError("Daily limit reached."))
        with pytest.raises(QuotaExceededError) as info:
            job.translate_document(request_for(source, output), provider, JobContext(), cache_dir=tmp_path / "c")
        assert "2 page(s) were already translated" in info.value.message and not output.exists()
        resumed = FakeProvider()
        job.translate_document(request_for(source, output), resumed, JobContext(), cache_dir=tmp_path / "c")
        assert resumed.text_calls == [["p3b0", "p3b1", "p3b2"]] and output.exists()
    finally:
        job.BATCH_CHARS = original_batch


def test_invalid_key_stops_immediately(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=2, with_image=False)
    provider = FakeProvider(fatal_on_call=1, fatal_error=ApiKeyError("Google did not accept the API key."))
    with pytest.raises(ApiKeyError):
        job.translate_document(request_for(source, tmp_path / "o.pdf"), provider, JobContext(), cache_dir=tmp_path / "c")


def test_cancel(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=3, with_image=False)
    job.BATCH_CHARS, original_batch = 250, job.BATCH_CHARS
    try:
        with pytest.raises(JobCancelled):
            job.translate_document(request_for(source, tmp_path / "o.pdf"), FakeProvider(cancel_on_call=2), JobContext(),
                                   cache_dir=tmp_path / "c")
        assert not (tmp_path / "o.pdf").exists()
    finally:
        job.BATCH_CHARS = original_batch


@pytest.mark.parametrize("content", [b"", b"not a pdf at all"])
def test_invalid_inputs(tmp_path, content):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(content)
    with pytest.raises(InvalidInputError):
        job.translate_document(request_for(bad, tmp_path / "o.pdf"), FakeProvider(), JobContext(), cache_dir=tmp_path)


def test_output_must_differ_from_input(tmp_path):
    source = make_text_pdf(tmp_path / "Book.pdf", pages=1)
    with pytest.raises(InvalidInputError):
        job.translate_document(request_for(source, source), FakeProvider(), JobContext(), cache_dir=tmp_path / "c")
    blank = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(blank))
    with pytest.raises(InvalidInputError, match="blank"):
        job.translate_document(request_for(blank, tmp_path / "o.pdf"), FakeProvider(), JobContext(), cache_dir=tmp_path)


# ----------------------------------------------------------------------------
# Manga / comics / scans
# ----------------------------------------------------------------------------
def test_cleanup_erases_bubble_lettering():
    image, bubbles, texts = make_manga_page()
    for bubble, text_box in zip(bubbles[:2], texts[:2], strict=True):
        plan = cleanup.plan_cleanup(image, text_box, "dialogue")
        assert plan.bubble and plan.background[0] > 240  # found the white bubble
        cleaned = image.copy()
        cleaned.paste(plan.patch, plan.patch_box[:2], plan.patch)
        gray = cleaned.crop(text_box).convert("L")
        assert gray.getextrema()[0] > 200, "letters must be covered"
        # the translation goes into the bubble, not outside it
        tx0, ty0, tx1, ty1 = plan.text_box
        assert bubble[0] <= tx0 < tx1 <= bubble[2] and bubble[1] <= ty0 < ty1 <= bubble[3]
        # artwork outside the bubble is untouched
        outside = (bubble[2] + 5, bubble[1], min(image.width, bubble[2] + 60), bubble[3])
        assert ImageChops.difference(image.crop(outside), cleaned.crop(outside)).getbbox() is None


def test_cleanup_on_open_artwork_covers_only_the_text_box():
    image = Image.new("RGB", (400, 300), (120, 90, 60))
    plan = cleanup.plan_cleanup(image, (100, 100, 200, 140), "sign")
    assert not plan.bubble and plan.background == (120, 90, 60)
    assert plan.patch_box[0] >= 90 and plan.patch_box[2] <= 210


def test_sound_effects_keep_artwork():
    image = Image.new("RGB", (400, 300), (120, 90, 60))
    plan = cleanup.plan_cleanup(image, (100, 100, 200, 140), "sfx")
    assert plan.label and plan.patch is None


def test_manga_pdf_translation_preserves_artwork(tmp_path):
    image, bubbles, texts = make_manga_page()
    source = image_pdf(tmp_path / "Chapter 01.pdf", image)
    before = sha256(source)
    output = tmp_path / "Chapter 01 - English.pdf"
    translations = ["Hi there!", "Where are you?", "Meanwhile..."]
    provider = FakeProvider(regions=regions_for(texts, image.size, translations))
    result = job.translate_document(request_for(source, output, doc_type="comic"), provider, JobContext(),
                                    cache_dir=tmp_path / "c")
    assert sha256(source) == before and output.exists() and "1 of 1" in result.message
    with pymupdf.open(str(output)) as doc, pymupdf.open(str(source)) as original:
        page = doc[0]
        assert page.rect == original[0].rect  # same page size
        scale = page.rect.width / image.width
        for bubble, text in zip(bubbles, translations, strict=True):
            hits = page.search_for(text.split()[0])
            assert hits, f"'{text}' was not placed"
            centre = ((hits[0].x0 + hits[0].x1) / 2 / scale, (hits[0].y0 + hits[0].y1) / 2 / scale)
            assert bubble[0] <= centre[0] <= bubble[2] and bubble[1] <= centre[1] <= bubble[3], "text left its bubble"
        # artwork away from the bubbles is identical
        zoom = pymupdf.Matrix(1 / scale, 1 / scale)
        new = page.get_pixmap(matrix=zoom, alpha=False)
        old = original[0].get_pixmap(matrix=zoom, alpha=False)
        new_img = Image.frombytes("RGB", (new.width, new.height), new.samples)
        old_img = Image.frombytes("RGB", (old.width, old.height), old.samples)
        corner = (900, 100, 990, 600)  # only artwork there
        diff = ImageChops.difference(new_img.crop(corner), old_img.crop(corner))
        assert max(diff.getextrema()[c][1] for c in range(3)) < 40


def test_image_folder_input(tmp_path):
    folder = tmp_path / "Manga Chapter 3"
    folder.mkdir()
    for number in (1, 2, 10):
        image, _, _ = make_manga_page((500, 750))
        image.save(folder / f"{number}.jpg", quality=90)
    output = tmp_path / "Manga Chapter 3 - Japanese.pdf"
    provider = FakeProvider(regions=[{"box": (0.1, 0.1, 0.4, 0.2), "kind": "dialogue", "original": "HELLO",
                                      "translation": "こんにちは"}])
    job.translate_document(request_for(folder, output, "ja"), provider, JobContext(), cache_dir=tmp_path / "c")
    with pymupdf.open(str(output)) as doc:
        assert doc.page_count == 3 and len(provider.image_calls) == 3
        assert "こんにちは" in doc[0].get_text().replace("\n", "")  # CJK may wrap between characters


def test_webtoon_strip_is_tiled():
    tiles = image_pages.tiles((1400, 9000))
    assert len(tiles) > 3 and tiles[0][0] == 0 and tiles[-1][1] == 9000
    cores = [(c0, c1) for _, _, c0, c1 in tiles]
    assert cores[0][0] == 0 and cores[-1][1] == 9000
    assert all(a[1] == b[0] for a, b in zip(cores, cores[1:], strict=False))  # cores cover the strip exactly once
    assert image_pages.tiles((1000, 1400)) == [(0, 1400, 0, 1400)]


def test_webtoon_regions_are_mapped_back(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=4000)  # tall strip
    page.draw_rect(page.rect, fill=(1, 1, 1))
    provider = FakeProvider(regions=[{"box": (0.4, 0.45, 0.6, 0.55), "kind": "dialogue", "original": "x",
                                      "translation": "y"}])
    regions = image_pages.analyze_page(page, provider, get_language("en"), None, "comic", True, JobContext())
    assert len(regions) == len(provider.image_calls) > 1
    centres = [(r.box[1] + r.box[3]) / 2 for r in regions]
    assert centres == sorted(centres) and centres[0] < 0.2 and centres[-1] > 0.8
    assert all(width == 1400 for width, _ in provider.image_calls)  # strips keep their width detail
