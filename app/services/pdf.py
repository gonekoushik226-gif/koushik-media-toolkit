"""PDF operations.

* Merge / split use pypdf. If pypdf cannot read a file (for example AES
  encryption with an empty password, or a damaged cross-reference table that
  MuPDF can repair), the operation automatically falls back to PyMuPDF.
* Rendering pages to images and building PDFs from images use PyMuPDF.
  Unmodified JPEG/PNG files are embedded byte-for-byte (no quality loss).
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from app.core.errors import InvalidInputError, ProcessingError
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.services.images import READ_ERRORS, ImageEdits, apply_edits, normalize_mode
from app.utils.filenames import numbered_names, temp_sibling
from app.utils.units import human_size

log = logging.getLogger(__name__)

POINTS_PER_INCH = 72.0
PAGE_SIZES = {  # points (1/72 inch), portrait
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
}
PAGE_SIZE_LABELS = {
    "image": "Same as each image",
    "a4": "A4 (auto portrait/landscape)",
    "letter": "US Letter (auto portrait/landscape)",
}
MAX_PAGE_POINTS = 14_400.0  # 200 inches, the PDF viewer limit
MAX_RENDER_PIXELS = 150_000_000


class _NeedsFallback(Exception):
    """pypdf could not handle a file; retry the operation with PyMuPDF."""


def _pymupdf():
    import pymupdf  # imported lazily: it is large and only needed here

    return pymupdf


# ----------------------------------------------------------------------------
# Split planning (pure functions)
# ----------------------------------------------------------------------------
def compute_equal_parts(total_pages: int, parts: int) -> list[tuple[int, int]]:
    """Split ``total_pages`` into ``parts`` ranges as equal as possible.
    Pages that do not divide evenly go to the first parts, e.g. 10 pages /
    3 parts -> 4, 3, 3. Ranges are 0-based, end exclusive."""
    if total_pages < 1:
        raise InvalidInputError("The PDF has no pages.")
    if not (1 <= parts <= total_pages):
        raise InvalidInputError(f"The number of parts must be between 1 and {total_pages} (the page count).")
    base, extra = divmod(total_pages, parts)
    ranges = []
    start = 0
    for index in range(parts):
        size = base + (1 if index < extra else 0)
        ranges.append((start, start + size))
        start += size
    return ranges


def compute_fixed_chunks(total_pages: int, pages_per_file: int) -> list[tuple[int, int]]:
    """Consecutive ranges of ``pages_per_file`` pages; the last may be shorter."""
    if total_pages < 1:
        raise InvalidInputError("The PDF has no pages.")
    if pages_per_file < 1:
        raise InvalidInputError("Pages per file must be at least 1.")
    return [(start, min(start + pages_per_file, total_pages)) for start in range(0, total_pages, pages_per_file)]


def describe_ranges(ranges: list[tuple[int, int]]) -> str:
    """'3 files: pages 1-4 (4), 5-7 (3), 8-10 (3)' - shown before splitting."""
    sizes = [end - start for start, end in ranges]
    if len(set(sizes)) == 1:
        head = f"{len(ranges)} file(s) of {sizes[0]} page(s) each"
    else:
        head = f"{len(ranges)} file(s) with " + ", ".join(str(s) for s in sizes) + " pages"
    detail = ", ".join(f"{s + 1}-{e}" if e - s > 1 else f"{s + 1}" for s, e in ranges[:12])
    if len(ranges) > 12:
        detail += ", ..."
    return f"{head} (pages {detail})"


# ----------------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------------
def _open_pypdf(path: Path):
    """Open with pypdf, returning a reader; raises _NeedsFallback if pypdf
    cannot handle the file."""
    import pypdf

    if not path.is_file():
        raise InvalidInputError(f"The file could not be found:\n{path}")
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            # pypdf could not open it with an empty password. MuPDF decides
            # (in the fallback) whether a real password is required.
            raise _NeedsFallback("encrypted")
        if len(reader.pages) == 0:
            raise InvalidInputError(f"'{path.name}' has no pages.")
        return reader
    except (InvalidInputError, _NeedsFallback):
        raise
    except Exception as exc:  # noqa: BLE001 - any pypdf problem (parse errors, missing AES support...) -> PyMuPDF
        raise _NeedsFallback(f"{type(exc).__name__}: {exc}") from exc


def open_pymupdf(path: Path):
    """Open with PyMuPDF; friendly errors for invalid/protected files."""
    pymupdf = _pymupdf()
    path = Path(path)
    if not path.is_file():
        raise InvalidInputError(f"The file could not be found:\n{path}")
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001 - MuPDF raises several types
        raise InvalidInputError(
            f"'{path.name}' is not a valid PDF or it is damaged.", details=f"{type(exc).__name__}: {exc}",
            title="Invalid PDF",
        ) from exc
    if not doc.is_pdf:
        doc.close()
        raise InvalidInputError(f"'{path.name}' is not a PDF file.", title="Invalid PDF")
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise InvalidInputError(
            f"'{path.name}' is protected with a password and cannot be opened.", title="Password-protected PDF"
        )
    if doc.page_count == 0:
        doc.close()
        raise InvalidInputError(f"'{path.name}' has no pages.")
    return doc


def count_pages(path: Path) -> int:
    doc = open_pymupdf(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _replace_from_temp(output: Path, write) -> None:
    """Call ``write(tmp_path)`` then move the temp file to ``output``."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(output)
    try:
        write(tmp)
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise ProcessingError("The PDF could not be written.")
        os.replace(tmp, output)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _verify_pdf(path: Path, expected_pages: int) -> None:
    doc = open_pymupdf(path)
    try:
        if doc.page_count != expected_pages:
            raise ProcessingError(
                "The written PDF has the wrong number of pages and was discarded.",
                details=f"expected {expected_pages}, found {doc.page_count}",
            )
    finally:
        doc.close()


# ----------------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------------
def merge_pdfs(sources: list[Path], output: Path, ctx: JobContext) -> JobResult:
    if len(sources) < 2:
        raise InvalidInputError("Please add at least two PDF files to merge.")
    for source in sources:
        if not Path(source).is_file():
            raise InvalidInputError(f"The file could not be found:\n{source}")
    ctx.set_status(f"Merging {len(sources)} PDFs...")
    try:
        pages = _merge_pypdf(sources, output, ctx)
    except _NeedsFallback as exc:
        log.info("pypdf could not merge (%s); using PyMuPDF", exc)
        ctx.set_status(f"Merging {len(sources)} PDFs (compatibility mode)...")
        pages = _merge_pymupdf(sources, output, ctx)
    return JobResult(f"Merged {len(sources)} PDFs into {output.name} ({pages} pages, {human_size(output.stat().st_size)})",
                     outputs=[output])


def _merge_pypdf(sources: list[Path], output: Path, ctx: JobContext) -> int:
    import pypdf

    writer = pypdf.PdfWriter()
    total = 0
    try:
        for number, source in enumerate(sources):
            ctx.check_cancelled()
            ctx.step(number, len(sources), f"Adding {Path(source).name} ({number + 1} / {len(sources)})")
            reader = _open_pypdf(Path(source))
            try:
                writer.append(reader)
            except Exception as exc:  # noqa: BLE001 - malformed objects inside the file
                raise _NeedsFallback(f"append failed for {source}: {exc}") from exc
            total += len(reader.pages)
        ctx.set_progress(None, "Saving...")
        _replace_from_temp(output, lambda tmp: _write_pypdf(writer, tmp))
    finally:
        writer.close()
    _verify_pdf(output, total)
    return total


def _write_pypdf(writer, tmp: Path) -> None:
    with open(tmp, "wb") as handle:
        writer.write(handle)


def _merge_pymupdf(sources: list[Path], output: Path, ctx: JobContext) -> int:
    pymupdf = _pymupdf()
    result = pymupdf.open()
    try:
        for number, source in enumerate(sources):
            ctx.check_cancelled()
            ctx.step(number, len(sources), f"Adding {Path(source).name} ({number + 1} / {len(sources)})")
            doc = open_pymupdf(Path(source))
            try:
                result.insert_pdf(doc)
            finally:
                doc.close()
        total = result.page_count
        ctx.set_progress(None, "Saving...")
        _replace_from_temp(output, lambda tmp: result.save(str(tmp), garbage=3, deflate=True))
    finally:
        result.close()
    _verify_pdf(output, total)
    return total


# ----------------------------------------------------------------------------
# Split
# ----------------------------------------------------------------------------
def split_pdf(
    source: Path, output_dir: Path, base_name: str, ranges: list[tuple[int, int]], ctx: JobContext
) -> JobResult:
    source = Path(source)
    if not ranges:
        raise InvalidInputError("Nothing to split.")
    output_dir.mkdir(parents=True, exist_ok=True)
    planned = [output_dir / name for name in numbered_names(base_name, len(ranges), "pdf", "part")]
    targets = ctx.resolve_outputs(planned)
    ctx.set_status(f"Splitting {source.name} into {len(ranges)} file(s)...")
    try:
        reader = _open_pypdf(source)
        total = len(reader.pages)
        _check_ranges(ranges, total)
        writer_fn = _split_part_pypdf(reader)
    except _NeedsFallback as exc:
        log.info("pypdf could not read %s (%s); using PyMuPDF", source, exc)
        doc = open_pymupdf(source)
        total = doc.page_count
        _check_ranges(ranges, total)
        writer_fn = _split_part_pymupdf(doc)
    created: list[Path] = []
    try:
        for number, ((start, end), target) in enumerate(zip(ranges, targets, strict=True)):
            ctx.check_cancelled()
            ctx.step(number, len(ranges), f"Part {number + 1} / {len(ranges)} (pages {start + 1}-{end})")
            _replace_from_temp(target, lambda tmp, s=start, e=end: writer_fn(s, e, tmp))
            created.append(target)
    except BaseException:
        # A half-finished split is confusing; remove the parts made so far.
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    ctx.step(len(ranges), len(ranges))
    return JobResult(f"Created {len(created)} PDF file(s) in {output_dir}", outputs=created,
                     details=describe_ranges(ranges))


def _check_ranges(ranges: list[tuple[int, int]], total: int) -> None:
    for start, end in ranges:
        if not (0 <= start < end <= total):
            raise InvalidInputError(f"Page range {start + 1}-{end} is outside the document (1-{total}).")


def _split_part_pypdf(reader):
    import pypdf

    def write(start: int, end: int, tmp: Path) -> None:
        writer = pypdf.PdfWriter()
        try:
            for index in range(start, end):
                writer.add_page(reader.pages[index])
            _write_pypdf(writer, tmp)
        finally:
            writer.close()

    return write


def _split_part_pymupdf(doc):
    pymupdf = _pymupdf()

    def write(start: int, end: int, tmp: Path) -> None:
        part = pymupdf.open()
        try:
            part.insert_pdf(doc, from_page=start, to_page=end - 1)
            part.save(str(tmp), garbage=3, deflate=True)
        finally:
            part.close()

    return write


# ----------------------------------------------------------------------------
# PDF -> images
# ----------------------------------------------------------------------------
def pdf_to_images(source: Path, output_dir: Path, prefix: str, image_format: str, dpi: int, ctx: JobContext) -> JobResult:
    image_format = image_format.lower()
    if image_format not in ("png", "jpg"):
        raise InvalidInputError("Choose PNG or JPEG.")
    if not (36 <= dpi <= 600):
        raise InvalidInputError("The resolution must be between 36 and 600 DPI.")
    source = Path(source)
    ctx.set_status(f"Opening {source.name}...")
    doc = open_pymupdf(source)
    warnings: list[str] = []
    created: list[Path] = []
    try:
        total = doc.page_count
        output_dir.mkdir(parents=True, exist_ok=True)
        planned = [output_dir / n for n in numbered_names(prefix, total, image_format, "page", min_width=3)]
        targets = ctx.resolve_outputs(planned)
        ctx.set_status(f"Converting {total} page(s) to {image_format.upper()}...")
        for index, target in enumerate(targets):
            ctx.check_cancelled()
            ctx.step(index, total, f"Page {index + 1} / {total}")
            page = doc.load_page(index)
            page_dpi = dpi
            pixels = (page.rect.width * dpi / POINTS_PER_INCH) * (page.rect.height * dpi / POINTS_PER_INCH)
            if pixels > MAX_RENDER_PIXELS:
                page_dpi = max(36, int(dpi * (MAX_RENDER_PIXELS / pixels) ** 0.5))
                warnings.append(f"Page {index + 1} is very large; it was rendered at {page_dpi} DPI instead of {dpi}.")
            pix = page.get_pixmap(dpi=page_dpi, alpha=False)
            if image_format == "jpg":
                _replace_from_temp(target, lambda tmp, p=pix: p.save(str(tmp), output="jpeg", jpg_quality=92))
            else:
                _replace_from_temp(target, lambda tmp, p=pix: p.save(str(tmp), output="png"))
            created.append(target)
        ctx.step(total, total)
    finally:
        doc.close()
    result = JobResult(f"Saved {len(created)} image(s) to {output_dir}", outputs=created)
    result.warnings.extend(warnings)
    return result


# ----------------------------------------------------------------------------
# Images -> PDF
# ----------------------------------------------------------------------------
@dataclass
class ImageSource:
    path: Path
    edits: ImageEdits | None = None


@dataclass
class _Payload:
    data: bytes
    width: int
    height: int
    dpi: float | None


def _image_dpi(img: Image.Image) -> float | None:
    dpi = img.info.get("dpi")
    try:
        value = float(dpi[0]) if dpi else None
    except (TypeError, ValueError, IndexError):
        return None
    return value if value and 50 <= value <= 1200 else None


def _encode_for_pdf(img: Image.Image, prefer_jpeg: bool) -> bytes:
    buffer = io.BytesIO()
    img = normalize_mode(img)
    if prefer_jpeg and img.mode in ("RGB", "L"):
        img.save(buffer, "JPEG", quality=92, optimize=True)
    else:
        img.save(buffer, "PNG", compress_level=6)
    return buffer.getvalue()


def _payloads(source: ImageSource) -> list[_Payload]:
    """Image data to embed: original bytes when possible (lossless), else a
    re-encoded upright image. Multi-page TIFFs give one payload per page."""
    path = Path(source.path)
    with Image.open(path) as img:
        fmt = img.format
        dpi = _image_dpi(img)
        frames = getattr(img, "n_frames", 1) or 1
        try:
            orientation = img.getexif().get(0x0112, 1)
        except Exception:  # noqa: BLE001
            orientation = 1
        unedited = source.edits is None or source.edits.is_identity
        if unedited and orientation in (0, 1) and frames == 1 and (
            (fmt == "JPEG" and img.mode in ("RGB", "L", "CMYK")) or (fmt == "PNG" and img.mode != "I;16")
        ):
            return [_Payload(path.read_bytes(), img.width, img.height, dpi)]
        prefer_jpeg = fmt in ("JPEG", "WEBP", "MPO")
        if fmt == "TIFF" and frames > 1:
            pages = [frame.copy() for frame in ImageSequence.Iterator(img)]
        else:
            img.load()
            pages = [img]
        payloads = []
        for page in pages:
            upright = ImageOps.exif_transpose(page)
            if source.edits is not None and not source.edits.is_identity:
                upright = apply_edits(normalize_mode(upright), source.edits)
            payloads.append(_Payload(_encode_for_pdf(upright, prefer_jpeg), upright.width, upright.height, dpi))
        return payloads


def _page_geometry(payload: _Payload, page_size: str, margin: float) -> tuple[float, float, tuple[float, float, float, float]]:
    """Page width/height and the image rectangle (x0, y0, x1, y1) in points."""
    if page_size == "image":
        dpi = payload.dpi or 96.0
        width = payload.width * POINTS_PER_INCH / dpi
        height = payload.height * POINTS_PER_INCH / dpi
        largest = max(width, height)
        if largest > MAX_PAGE_POINTS:
            width, height = width * MAX_PAGE_POINTS / largest, height * MAX_PAGE_POINTS / largest
        return width, height, (0.0, 0.0, width, height)
    page_w, page_h = PAGE_SIZES[page_size]
    if payload.width > payload.height:
        page_w, page_h = page_h, page_w  # landscape image -> landscape page
    box_w, box_h = page_w - 2 * margin, page_h - 2 * margin
    ratio = min(box_w / payload.width, box_h / payload.height)
    img_w, img_h = payload.width * ratio, payload.height * ratio
    x0 = (page_w - img_w) / 2
    y0 = (page_h - img_h) / 2
    return page_w, page_h, (x0, y0, x0 + img_w, y0 + img_h)


def images_to_pdf(
    sources: list[ImageSource], output: Path, page_size: str, ctx: JobContext, margin_mm: float = 10.0
) -> JobResult:
    if not sources:
        raise InvalidInputError("Please add at least one image.")
    if page_size not in PAGE_SIZE_LABELS:
        raise InvalidInputError(f"Unknown page size: {page_size}")
    pymupdf = _pymupdf()
    margin = 0.0 if page_size == "image" else margin_mm * POINTS_PER_INCH / 25.4
    doc = pymupdf.open()
    skipped: list[str] = []
    try:
        ctx.set_status(f"Creating PDF from {len(sources)} image(s)...")
        for number, source in enumerate(sources):
            ctx.check_cancelled()
            ctx.step(number, len(sources), f"Image {number + 1} / {len(sources)}: {Path(source.path).name}")
            try:
                payloads = _payloads(source)
                for payload in payloads:
                    width, height, rect = _page_geometry(payload, page_size, margin)
                    page = doc.new_page(width=width, height=height)
                    page.insert_image(pymupdf.Rect(*rect), stream=payload.data, keep_proportion=True)
            except READ_ERRORS as exc:
                skipped.append(f"{Path(source.path).name}: {_short_reason(exc)}")
            except RuntimeError as exc:  # MuPDF could not decode the image data
                skipped.append(f"{Path(source.path).name}: {_short_reason(exc)}")
        if doc.page_count == 0:
            raise InvalidInputError(
                "None of the images could be read, so no PDF was created.", details="\n".join(skipped)
            )
        pages = doc.page_count
        ctx.set_progress(None, "Saving PDF...")
        _replace_from_temp(output, lambda tmp: doc.save(str(tmp), garbage=3, deflate=True))
    finally:
        doc.close()
    _verify_pdf(output, pages)
    result = JobResult(f"Created {output.name} with {pages} page(s) ({human_size(output.stat().st_size)})",
                       outputs=[output])
    if skipped:
        result.warnings.append(f"{len(skipped)} file(s) could not be read and were skipped:\n" + "\n".join(skipped))
    return result


def _short_reason(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    if "cannot identify image file" in text:
        return "not a readable image"
    return text.splitlines()[0][:160]


# ----------------------------------------------------------------------------
# Information
# ----------------------------------------------------------------------------
def _pdf_date(value: str) -> str:
    """'D:20240131120000+01'00'' -> '2024-01-31 12:00'."""
    match = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?", value or "")
    if not match:
        return value or "-"
    year, month, day, hour, minute = match.groups()
    text = year
    if month:
        text += f"-{month}"
    if day:
        text += f"-{day}"
    if hour:
        text += f" {hour}:{minute or '00'}"
    return text


def pdf_summary(path: Path) -> str:
    path = Path(path)
    doc = open_pymupdf(path)
    try:
        meta = doc.metadata or {}
        first = doc.load_page(0).rect
        width_mm, height_mm = first.width / POINTS_PER_INCH * 25.4, first.height / POINTS_PER_INCH * 25.4
        lines = [
            f"File:        {path.name}",
            f"Folder:      {path.parent}",
            f"Pages:       {doc.page_count}",
            f"File size:   {human_size(path.stat().st_size)}",
            f"Page size:   {width_mm:.0f} x {height_mm:.0f} mm (first page)",
            f"PDF version: {meta.get('format') or '-'}",
            f"Encrypted:   {meta.get('encryption') or 'No'}",
            f"Title:       {meta.get('title') or '-'}",
            f"Author:      {meta.get('author') or '-'}",
            f"Subject:     {meta.get('subject') or '-'}",
            f"Keywords:    {meta.get('keywords') or '-'}",
            f"Created by:  {meta.get('creator') or '-'}",
            f"Producer:    {meta.get('producer') or '-'}",
            f"Created:     {_pdf_date(meta.get('creationDate', ''))}",
            f"Modified:    {_pdf_date(meta.get('modDate', ''))}",
        ]
        return "\n".join(lines)
    finally:
        doc.close()
