"""The complete translation workflow for one document.

    source PDF / image folder
      -> analyse pages (text / image / blank)
      -> translate text pages in batches, image pages one by one (tiles for strips)
         (every finished page is saved in a resumable progress cache)
      -> build a NEW PDF: <name> - <Language>.pdf   (the original is never modified)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.config import paths
from app.core.errors import InvalidInputError
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.services import pdf as pdf_service
from app.services.images import list_images_in_folder
from app.services.translation import image_pages, prompts, text_pages
from app.services.translation.languages import Language
from app.services.translation.models import PagePlan, Region
from app.services.translation.provider import (
    FATAL_ERRORS,
    PageError,
    ResponseTooLongError,
    TranslationError,
    TranslationProvider,
)
from app.utils.filenames import build_filename, same_path, temp_sibling

log = logging.getLogger(__name__)

DOC_TYPES = {
    "auto": "Automatic (decide for each page)",
    "text": "Book or document with selectable text",
    "comic": "Manga, comic or webtoon",
    "scan": "Scanned book or document (page images)",
}
BATCH_CHARS = 6000  # source characters per text request
CONTEXT_CHARS = 600  # preceding text sent along for continuity
LARGE_DOCUMENT_PAGES = 100


@dataclass
class TranslationRequest:
    source: Path  # a PDF file or a folder of images
    output: Path
    target: Language
    source_language: Language | None = None
    doc_type: str = "auto"
    pages: list[int] | None = None  # 0-based pages to translate; None = all
    include_sfx: bool = True
    model: str = ""


@dataclass
class DocumentPlan:
    total_pages: int
    pages: list[PagePlan] = field(default_factory=list)

    @property
    def text_pages(self) -> list[PagePlan]:
        return [p for p in self.pages if p.mode == "text"]

    @property
    def image_pages(self) -> list[PagePlan]:
        return [p for p in self.pages if p.mode == "image"]

    def describe(self) -> str:
        """'10 with selectable text, 2 image/scanned' (for the pages in the plan)."""
        parts = []
        if self.text_pages:
            parts.append(f"{len(self.text_pages)} with selectable text")
        if self.image_pages:
            parts.append(f"{len(self.image_pages)} image/scanned")
        blank = len(self.pages) - len(self.text_pages) - len(self.image_pages)
        if blank:
            parts.append(f"{blank} blank")
        return ", ".join(parts) or "no pages"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def default_output_name(source: Path, target: Language) -> str:
    """'Japanese Book.pdf' -> 'Japanese Book - English' (without extension)."""
    source = Path(source)
    stem = pdf_service.pdf_name_for_folder(source) if source.is_dir() else source.stem
    return f"{stem} - {target.name}"


def parse_page_range(text: str, total: int) -> list[int] | None:
    """'1-5, 8, 10-12' -> 0-based sorted pages. Empty/'all' -> None (all pages)."""
    value = (text or "").strip().lower()
    if not value or value in ("all", "*"):
        return None
    pages: set[int] = set()
    for part in re.split(r"[,;\s]+", value):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise InvalidInputError(f"'{part}' is not a valid page or page range. Example: 1-5, 8, 10-12")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > total:
            raise InvalidInputError(f"Pages must be between 1 and {total}; '{part}' is outside that range.")
        pages.update(range(first - 1, last))
    return sorted(pages)


def _page_has_graphics(page) -> bool:
    try:
        return bool(page.get_images(full=False)) or bool(page.get_drawings())
    except Exception:  # noqa: BLE001
        return True


def plan_document(doc, doc_type: str, pages: list[int] | None) -> DocumentPlan:
    total = doc.page_count
    selected = pages if pages is not None else list(range(total))
    plan = DocumentPlan(total_pages=total)
    for index in selected:
        page = doc[index]
        if doc_type in ("comic", "scan"):
            plan.pages.append(PagePlan(index, "image"))
            continue
        visible, _invisible = text_pages.text_visibility(page)
        if visible >= text_pages.MIN_TEXT_CHARS and page.rotation == 0:
            segments = text_pages.extract_segments(page, index)
            if segments:
                plan.pages.append(PagePlan(index, "text", segments, sum(len(s.text) for s in segments)))
                continue
        if _page_has_graphics(page) or visible:
            plan.pages.append(PagePlan(index, "image"))
        else:
            plan.pages.append(PagePlan(index, "skip"))
    return plan


def analyze_source(source: Path, doc_type: str = "auto", pages: list[int] | None = None) -> DocumentPlan:
    """Quick look at a document (used by the UI before translating)."""
    source = Path(source)
    if source.is_dir():
        images = list_images_in_folder(source)
        if not images:
            raise InvalidInputError("The folder does not contain any images.")
        return DocumentPlan(total_pages=len(images), pages=[PagePlan(i, "image") for i in range(len(images))])
    doc = pdf_service.open_pymupdf(source)
    try:
        return plan_document(doc, doc_type, pages)
    finally:
        doc.close()


# ----------------------------------------------------------------------------
# Progress cache (resume)
# ----------------------------------------------------------------------------
def cache_root() -> Path:
    return paths.data_dir() / "translation-progress"


def clear_cache() -> None:
    shutil.rmtree(cache_root(), ignore_errors=True)


def _fingerprint(source: Path) -> str:
    digest = hashlib.sha256()
    if source.is_dir():
        for image in list_images_in_folder(source):
            stat = image.stat()
            digest.update(f"{image.name}|{stat.st_size}|{int(stat.st_mtime)}\n".encode())
    else:
        with open(source, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    return digest.hexdigest()


class ProgressCache:
    """Finished pages of one (document, language, settings) combination."""

    def __init__(self, request: TranslationRequest, root: Path | None = None):
        key = hashlib.sha256(json.dumps([
            _fingerprint(request.source), request.target.code,
            request.source_language.code if request.source_language else "", request.doc_type, request.model,
            request.include_sfx, prompts.PROMPT_VERSION,
        ]).encode()).hexdigest()[:32]
        self.folder = (root or cache_root()) / key
        self.folder.mkdir(parents=True, exist_ok=True)
        meta = self.folder / "about.json"
        if not meta.exists():
            meta.write_text(json.dumps({"document": Path(request.source).name, "language": request.target.name,
                                        "created": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
                            encoding="utf-8")

    def _file(self, index: int) -> Path:
        return self.folder / f"page-{index + 1:05d}.json"

    def load(self, index: int) -> dict | None:
        try:
            return json.loads(self._file(index).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, index: int, data: dict) -> None:
        target = self._file(index)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)

    def discard(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


# ----------------------------------------------------------------------------
# Translation
# ----------------------------------------------------------------------------
@dataclass
class _Batch:
    pages: list[PagePlan]
    segments: list  # TextSegment
    first_page: int


def _text_batches(pending: list[PagePlan]) -> list[_Batch]:
    batches: list[_Batch] = []
    current: list[PagePlan] = []
    size = 0
    for plan in pending:
        if current and size + plan.chars > BATCH_CHARS:
            batches.append(_Batch(current, [s for p in current for s in p.segments], current[0].index))
            current, size = [], 0
        if plan.chars > BATCH_CHARS:  # a very full page: split it over several requests
            chunk, chunk_size = [], 0
            for segment in plan.segments:
                if chunk and chunk_size + len(segment.text) > BATCH_CHARS:
                    batches.append(_Batch([plan], chunk, plan.index))
                    chunk, chunk_size = [], 0
                chunk.append(segment)
                chunk_size += len(segment.text)
            if chunk:
                batches.append(_Batch([plan], chunk, plan.index))
            continue
        current.append(plan)
        size += plan.chars
    if current:
        batches.append(_Batch(current, [s for p in current for s in p.segments], current[0].index))
    return batches


class _Run:
    def __init__(self, request: TranslationRequest, provider: TranslationProvider, cache: ProgressCache, ctx: JobContext,
                 total: int):
        self.request = request
        self.provider = provider
        self.cache = cache
        self.ctx = ctx
        self.total = total
        self.done = 0
        self.results: dict[int, dict] = {}
        self.failed: dict[int, str] = {}
        self.context = ""

    def _progress(self, page_index: int, what: str) -> None:
        self.ctx.set_status(f"{what} page {page_index + 1} ({min(self.done + 1, self.total)} of {self.total})...")
        self.ctx.step(self.done, self.total, f"{self.done} of {self.total} page(s) finished")

    def translate_segments(self, segments: list) -> dict[str, str]:
        """Translate, halving the request when the answer would be too long."""
        payload = [{"id": s.id, "text": s.text} for s in segments]
        try:
            return self.provider.translate_segments(payload, self.request.target, self.request.source_language,
                                                    self.context, self.ctx)
        except ResponseTooLongError:
            if len(segments) == 1:
                raise
            middle = len(segments) // 2
            result = self.translate_segments(segments[:middle])
            result.update(self.translate_segments(segments[middle:]))
            return result

    def run_text_batch(self, batch: _Batch, partial: dict[int, dict]) -> None:
        self._progress(batch.first_page, "Translating")
        try:
            translations = self.translate_segments(batch.segments)
        except PageError as exc:
            if len(batch.pages) > 1:  # find out which page is the problem
                for plan in batch.pages:
                    self.run_text_batch(_Batch([plan], plan.segments, plan.index), partial)
                return
            for plan in batch.pages:
                self.failed[plan.index] = exc.message
                self.done += 1
            return
        self.context = " ".join(s.text for s in batch.segments)[-CONTEXT_CHARS:]
        for plan in batch.pages:
            entry = partial.setdefault(plan.index, {"mode": "text", "translations": {}})
            entry["translations"].update({k: v for k, v in translations.items()
                                          if k in {s.id for s in plan.segments}})
            if all(s.id in entry["translations"] for s in plan.segments):
                self.cache.save(plan.index, entry)
                self.results[plan.index] = entry
                self.done += 1

    def run_image_page(self, doc, plan: PagePlan) -> None:
        self._progress(plan.index, "Reading and translating")
        try:
            regions = image_pages.analyze_page(doc[plan.index], self.provider, self.request.target,
                                               self.request.source_language, self.request.doc_type,
                                               self.request.include_sfx, self.ctx)
        except PageError as exc:
            self.failed[plan.index] = exc.message
            self.done += 1
            return
        entry = {"mode": "image", "regions": [r.to_json() for r in regions]}
        self.cache.save(plan.index, entry)
        self.results[plan.index] = entry
        self.done += 1


def _fatal_with_progress(exc: TranslationError, run: _Run) -> TranslationError:
    finished = len(run.results)
    if finished:
        exc.message += (f"\n\n{finished} page(s) were already translated and are saved. Run the same translation again "
                        "(same file and language) to continue where it stopped - finished pages are not sent again.")
    return exc


def translate_document(request: TranslationRequest, provider: TranslationProvider, ctx: JobContext,
                       cache_dir: Path | None = None) -> JobResult:
    source, output = Path(request.source), Path(request.output)
    if not source.exists():
        raise InvalidInputError(f"The file or folder could not be found:\n{source}")
    if output.suffix.lower() != ".pdf":
        raise InvalidInputError("The translated file must be saved as a .pdf file.")
    if same_path(source, output):
        raise InvalidInputError("The translation cannot replace the original document. Choose a different file name.")
    workdir = Path(tempfile.mkdtemp(prefix="mt-translate-"))
    warnings: list[str] = []
    try:
        ctx.set_status("Analyzing document...")
        ctx.set_progress(None)
        if source.is_dir():
            images = list_images_in_folder(source)
            if not images:
                raise InvalidInputError("The folder does not contain any images.")
            base_pdf = workdir / "pages.pdf"
            built = pdf_service.images_to_pdf([pdf_service.ImageSource(p) for p in images], base_pdf, "image", ctx)
            warnings.extend(built.warnings)
            doc_type = "comic" if request.doc_type in ("auto", "text") else request.doc_type
        else:
            base_pdf = source
            doc_type = request.doc_type
        doc = pdf_service.open_pymupdf(base_pdf)
        try:
            return _translate_open_document(doc, request, doc_type, provider, ctx, cache_dir, output, warnings)
        finally:
            doc.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _translate_open_document(doc, request: TranslationRequest, doc_type: str, provider: TranslationProvider,
                             ctx: JobContext, cache_dir: Path | None, output: Path, warnings: list[str]) -> JobResult:
    ctx.set_status("Analyzing pages...")
    plan = plan_document(doc, doc_type, request.pages)
    work = [p for p in plan.pages if p.mode != "skip"]
    if not work:
        raise InvalidInputError("There is nothing to translate: the selected pages are blank.")
    cache = ProgressCache(request, cache_dir)
    run = _Run(request, provider, cache, ctx, len(work))
    for page_plan in work:  # reuse finished pages from an earlier run
        cached = cache.load(page_plan.index)
        if cached and cached.get("mode") == page_plan.mode:
            run.results[page_plan.index] = cached
            run.done += 1
    if run.done:
        log.info("Resuming translation: %s of %s page(s) already done", run.done, len(work))
    pending_text = [p for p in work if p.mode == "text" and p.index not in run.results]
    pending_images = [p for p in work if p.mode == "image" and p.index not in run.results]
    units: list[tuple[int, object]] = [(b.first_page, b) for b in _text_batches(pending_text)]
    units += [(p.index, p) for p in pending_images]
    units.sort(key=lambda unit: unit[0])
    partial: dict[int, dict] = {}
    try:
        for _, unit in units:
            ctx.check_cancelled()
            if isinstance(unit, _Batch):
                run.run_text_batch(unit, partial)
            else:
                run.run_image_page(doc, unit)
    except FATAL_ERRORS as exc:
        raise _fatal_with_progress(exc, run) from None
    if not run.results:
        details = "\n".join(f"Page {i + 1}: {message}" for i, message in sorted(run.failed.items()))
        raise TranslationError("No page could be translated.", details=details)

    ctx.set_status("Creating the translated PDF...")
    selected = [p.index for p in plan.pages]
    if request.pages is not None:
        doc.select(selected)
    position = {original: number for number, original in enumerate(selected)}
    translated_blocks = 0
    for number, page_plan in enumerate(work):
        ctx.check_cancelled()
        ctx.step(number, len(work), f"Building page {number + 1} of {len(work)}")
        entry = run.results.get(page_plan.index)
        if entry is None:
            continue
        page_number = position[page_plan.index]
        if entry["mode"] == "text":
            translated_blocks += text_pages.apply_translations(doc[page_number], page_plan.segments,
                                                               entry.get("translations", {}), request.target)
        else:
            page = doc[page_number]
            if page.rotation:
                page = image_pages.flatten_rotated_page(doc, page_number)
            regions = [Region.from_json(r) for r in entry.get("regions", [])]
            translated_blocks += image_pages.apply_regions(page, regions, request.target)
    _save_new_pdf(doc, output, ctx)
    if not run.failed:
        cache.discard()  # everything done - no need to keep progress
    else:
        pages = ", ".join(str(i + 1) for i in sorted(run.failed))
        warnings.append(
            f"{len(run.failed)} page(s) could not be translated and were left unchanged: {pages}.\n"
            + "\n".join(f"Page {i + 1}: {m}" for i, m in sorted(run.failed.items()))
            + "\n\nRun the translation again to retry only these pages.")
    result = JobResult(
        f"Created {output.name}: {len(run.results)} of {len(work)} page(s) translated into {request.target.name}",
        outputs=[output],
        warnings=warnings,
        details=f"{translated_blocks} text block(s) replaced. {provider.usage.describe()}",
    )
    return result


def _save_new_pdf(doc, output: Path, ctx: JobContext) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(output)
    try:
        ctx.set_progress(None, "Saving...")
        doc.save(str(tmp), garbage=3, deflate=True)
        with pymupdf.open(str(tmp)) as check:
            if check.page_count != doc.page_count:
                raise TranslationError("The translated PDF could not be written correctly.")
        os.replace(tmp, output)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def output_path_for(source: Path, folder: Path, target: Language) -> Path:
    return Path(folder) / build_filename(default_output_name(source, target), "pdf")

