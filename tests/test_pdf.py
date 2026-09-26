import pytest
from PIL import Image

from app.core.errors import InvalidInputError
from app.core.jobs import JobContext
from app.services import pdf as svc
from app.services.images import ImageEdits, rotate_op
from tests.conftest import make_image, pdf_texts


class TestSplitPlanning:
    def test_equal_parts_exact(self):
        ranges = svc.compute_equal_parts(500, 10)
        assert len(ranges) == 10 and all(end - start == 50 for start, end in ranges)
        assert ranges[0] == (0, 50) and ranges[-1] == (450, 500)

    def test_equal_parts_with_remainder(self):
        ranges = svc.compute_equal_parts(10, 3)
        assert [end - start for start, end in ranges] == [4, 3, 3]
        assert ranges == [(0, 4), (4, 7), (7, 10)]

    def test_equal_parts_cover_every_page_once(self):
        for total in range(1, 40):
            for parts in range(1, total + 1):
                ranges = svc.compute_equal_parts(total, parts)
                pages = [p for start, end in ranges for p in range(start, end)]
                assert pages == list(range(total))
                sizes = [e - s for s, e in ranges]
                assert max(sizes) - min(sizes) <= 1

    def test_equal_parts_invalid(self):
        with pytest.raises(InvalidInputError):
            svc.compute_equal_parts(5, 6)
        with pytest.raises(InvalidInputError):
            svc.compute_equal_parts(5, 0)

    def test_fixed_chunks(self):
        assert svc.compute_fixed_chunks(500, 125) == [(0, 125), (125, 250), (250, 375), (375, 500)]
        assert svc.compute_fixed_chunks(10, 4) == [(0, 4), (4, 8), (8, 10)]
        assert svc.compute_fixed_chunks(3, 10) == [(0, 3)]
        with pytest.raises(InvalidInputError):
            svc.compute_fixed_chunks(10, 0)

    def test_describe_ranges(self):
        assert svc.describe_ranges([(0, 4), (4, 7), (7, 10)]) == "3 file(s) with 4, 3, 3 pages (pages 1-4, 5-7, 8-10)"
        assert svc.describe_ranges([(0, 2), (2, 4)]).startswith("2 file(s) of 2 page(s) each")


class TestMerge:
    def test_merge_in_order(self, pdf_factory, tmp_path):
        a = pdf_factory("a.pdf", 2, "A")
        b = pdf_factory("b.pdf", 1, "B")
        out = tmp_path / "merged.pdf"
        result = svc.merge_pdfs([b, a], out, JobContext())
        assert pdf_texts(out) == ["B 1", "A 1", "A 2"]
        assert "3 pages" in result.message

    def test_merge_needs_two(self, pdf_factory, tmp_path):
        with pytest.raises(InvalidInputError):
            svc.merge_pdfs([pdf_factory()], tmp_path / "x.pdf", JobContext())

    def test_invalid_pdf(self, pdf_factory, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"this is not a pdf at all")
        out = tmp_path / "out.pdf"
        with pytest.raises(InvalidInputError, match="bad.pdf"):
            svc.merge_pdfs([pdf_factory(), bad], out, JobContext())
        assert not out.exists() and not [p for p in tmp_path.iterdir() if "mt-partial" in p.name]

    def test_encrypted_with_empty_password_uses_fallback(self, pdf_factory, tmp_path):
        import pymupdf

        source = pdf_factory("plain.pdf", 2, "S")
        locked = tmp_path / "locked.pdf"
        with pymupdf.open(str(source)) as doc:
            doc.save(str(locked), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="",
                     permissions=pymupdf.PDF_PERM_PRINT)
        out = tmp_path / "merged.pdf"
        svc.merge_pdfs([locked, source], out, JobContext())
        assert pdf_texts(out) == ["S 1", "S 2", "S 1", "S 2"]

    def test_password_protected_is_reported(self, pdf_factory, tmp_path):
        import pymupdf

        source = pdf_factory("plain.pdf", 1)
        locked = tmp_path / "secret.pdf"
        with pymupdf.open(str(source)) as doc:
            doc.save(str(locked), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="user")
        with pytest.raises(InvalidInputError, match="password"):
            svc.merge_pdfs([locked, source], tmp_path / "m.pdf", JobContext())

    def test_cancel(self, pdf_factory, tmp_path):
        ctx = JobContext()
        ctx.cancel()
        from app.core.errors import JobCancelled

        with pytest.raises(JobCancelled):
            svc.merge_pdfs([pdf_factory("a.pdf"), pdf_factory("b.pdf")], tmp_path / "m.pdf", ctx)
        assert not (tmp_path / "m.pdf").exists()


class TestSplit:
    def test_split_equal(self, pdf_factory, tmp_path):
        source = pdf_factory("doc.pdf", 10)
        out_dir = tmp_path / "parts"
        result = svc.split_pdf(source, out_dir, "document", svc.compute_equal_parts(10, 3), JobContext())
        names = [p.name for p in result.outputs]
        assert names == ["document_part01.pdf", "document_part02.pdf", "document_part03.pdf"]
        assert pdf_texts(out_dir / "document_part01.pdf") == ["Page 1", "Page 2", "Page 3", "Page 4"]
        assert pdf_texts(out_dir / "document_part03.pdf") == ["Page 8", "Page 9", "Page 10"]

    def test_split_fixed(self, pdf_factory, tmp_path):
        source = pdf_factory("doc.pdf", 5)
        result = svc.split_pdf(source, tmp_path / "o", "x", svc.compute_fixed_chunks(5, 2), JobContext())
        assert [len(pdf_texts(p)) for p in result.outputs] == [2, 2, 1]

    def test_split_respects_existing_files(self, pdf_factory, tmp_path):
        source = pdf_factory("doc.pdf", 2)
        out_dir = tmp_path / "o"
        out_dir.mkdir()
        (out_dir / "x_part01.pdf").write_text("keep")
        result = svc.split_pdf(source, out_dir, "x", svc.compute_fixed_chunks(2, 1), JobContext())
        assert (out_dir / "x_part01.pdf").read_text() == "keep"
        assert result.outputs[0].name == "x_part01 (1).pdf"

    def test_invalid_range(self, pdf_factory, tmp_path):
        with pytest.raises(InvalidInputError):
            svc.split_pdf(pdf_factory("d.pdf", 2), tmp_path, "x", [(0, 5)], JobContext())


class TestPdfToImages:
    @pytest.mark.parametrize("fmt, pil_format", [("png", "PNG"), ("jpg", "JPEG")])
    def test_render(self, pdf_factory, tmp_path, fmt, pil_format):
        source = pdf_factory("doc.pdf", 3)
        progress = []
        ctx = JobContext(on_progress=lambda f, d: progress.append(d), min_interval=0)
        result = svc.pdf_to_images(source, tmp_path / "img", "doc", fmt, 72, ctx)
        assert [p.name for p in result.outputs] == [f"doc_page001.{fmt}", f"doc_page002.{fmt}", f"doc_page003.{fmt}"]
        with Image.open(result.outputs[0]) as img:
            assert img.format == pil_format and img.size == (300, 400)
        assert "Page 2 / 3" in progress

    def test_dpi_scales_output(self, pdf_factory, tmp_path):
        result = svc.pdf_to_images(pdf_factory("d.pdf", 1), tmp_path, "d", "png", 144, JobContext())
        with Image.open(result.outputs[0]) as img:
            assert img.size == (600, 800)

    def test_invalid_input(self, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 garbage")
        with pytest.raises(InvalidInputError):
            svc.pdf_to_images(bad, tmp_path, "x", "png", 72, JobContext())


class TestImagesToPdf:
    def test_multi_page_with_orientation(self, tmp_path):
        a = make_image(tmp_path / "landscape.png", size=(200, 100))
        b = make_image(tmp_path / "phone.jpg", size=(200, 100), orientation=6)  # displayed as 100x200
        out = tmp_path / "out.pdf"
        result = svc.images_to_pdf([svc.ImageSource(a), svc.ImageSource(b)], out, "image", JobContext())
        import pymupdf

        with pymupdf.open(str(out)) as doc:
            assert doc.page_count == 2
            assert doc[0].rect.width > doc[0].rect.height  # landscape
            assert doc[1].rect.height > doc[1].rect.width  # EXIF rotation respected
        assert "2 page(s)" in result.message

    def test_jpeg_is_embedded_without_recompression(self, tmp_path):
        import pymupdf

        photo = tmp_path / "photo.jpg"
        Image.effect_noise((300, 200), 60).convert("RGB").save(photo, quality=95)
        out = tmp_path / "out.pdf"
        svc.images_to_pdf([svc.ImageSource(photo)], out, "image", JobContext())
        with pymupdf.open(str(out)) as doc:
            xref = doc[0].get_images()[0][0]
            assert doc.extract_image(xref)["image"] == photo.read_bytes()

    def test_a4_pages_and_edits(self, tmp_path):
        img = make_image(tmp_path / "wide.png", size=(300, 100))
        out = tmp_path / "a4.pdf"
        svc.images_to_pdf([svc.ImageSource(img, ImageEdits([rotate_op(90)]))], out, "a4", JobContext())
        import pymupdf

        with pymupdf.open(str(out)) as doc:
            assert doc[0].rect.width == pytest.approx(595.28, abs=0.5)  # rotated -> portrait A4

    def test_unreadable_images_are_reported(self, tmp_path):
        good = make_image(tmp_path / "good.png")
        bad = tmp_path / "broken.jpg"
        bad.write_text("nope")
        out = tmp_path / "out.pdf"
        result = svc.images_to_pdf([svc.ImageSource(bad), svc.ImageSource(good)], out, "image", JobContext())
        assert result.warnings and "broken.jpg" in result.warnings[0]
        assert "1 page(s)" in result.message

    def test_all_unreadable(self, tmp_path):
        bad = tmp_path / "broken.jpg"
        bad.write_text("nope")
        with pytest.raises(InvalidInputError):
            svc.images_to_pdf([svc.ImageSource(bad)], tmp_path / "o.pdf", "image", JobContext())
        assert not (tmp_path / "o.pdf").exists()

    def test_multipage_tiff(self, tmp_path):
        tiff = tmp_path / "scan.tiff"
        pages = [Image.new("RGB", (50, 70), c) for c in ("red", "green", "blue")]
        pages[0].save(tiff, save_all=True, append_images=pages[1:])
        out = tmp_path / "scan.pdf"
        svc.images_to_pdf([svc.ImageSource(tiff)], out, "image", JobContext())
        assert svc.count_pages(out) == 3


def test_pdf_summary(pdf_factory):
    text = svc.pdf_summary(pdf_factory("info.pdf", 4))
    assert "Pages:       4" in text and "info.pdf" in text


def test_pdf_date():
    assert svc._pdf_date("D:20240131120000+01'00'") == "2024-01-31 12:00"
    assert svc._pdf_date("") == "-"
