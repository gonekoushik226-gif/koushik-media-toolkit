"""Image operations with Pillow.

Edits are recorded non-destructively as a list of operations
(:class:`ImageEdits`) and applied when saving, so the preview and the final
file always match and the original is never modified. Crop boxes are stored
as fractions of the image, which makes the same edit list work on the
downscaled preview and on the full-resolution original.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

from app.core.errors import InvalidInputError, ProcessingError
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.utils.filenames import build_filename, temp_sibling
from app.utils.sorting import natural_key
from app.utils.units import human_size

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 400_000_000  # allow big scans, still stop decompression bombs

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".ico")
OUTPUT_FORMATS = {
    "keep": "Same as original",
    "jpg": "JPG",
    "png": "PNG",
    "webp": "WEBP",
    "bmp": "BMP",
    "tiff": "TIFF",
    "gif": "GIF",
    "ico": "ICO (icon)",
}
PIL_FORMATS = {
    "jpg": "JPEG", "jpeg": "JPEG", "jfif": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP",
    "tif": "TIFF", "tiff": "TIFF", "gif": "GIF", "ico": "ICO",
}
LOSSY_FORMATS = {"JPEG", "WEBP"}
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
READ_ERRORS = (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, EOFError)
_EXIF_ORIENTATION = 0x0112
_EXIF_IFD = 0x8769
_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME = 306


def is_image_path(path: Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def list_images_in_folder(folder: Path, recursive: bool = False) -> list[Path]:
    folder = Path(folder)
    pattern = "**/*" if recursive else "*"
    found = [p for p in folder.glob(pattern) if p.is_file() and is_image_path(p)]
    return sorted(found, key=lambda p: natural_key(str(p.relative_to(folder))))


# ----------------------------------------------------------------------------
# Edit model
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class ImageOp:
    kind: str  # rotate | flip | crop | resize
    value: tuple

    def describe(self) -> str:
        if self.kind == "rotate":
            return {90: "Rotate 90° right", 180: "Rotate 180°", 270: "Rotate 90° left"}.get(self.value[0], "Rotate")
        if self.kind == "flip":
            return "Flip horizontally" if self.value[0] == "h" else "Flip vertically"
        if self.kind == "crop":
            return "Crop"
        if self.kind == "resize":
            mode, a, b = self.value
            if mode == "percent":
                return f"Resize {a:g}%"
            if mode == "fit":
                return f"Fit within {a}x{b}"
            return f"Resize to {a}x{b}"
        return self.kind


def rotate_op(degrees_clockwise: int) -> ImageOp:
    degrees = degrees_clockwise % 360
    if degrees not in (90, 180, 270):
        raise ValueError("rotation must be a multiple of 90 degrees")
    return ImageOp("rotate", (degrees,))


def flip_op(axis: str) -> ImageOp:
    if axis not in ("h", "v"):
        raise ValueError("axis must be 'h' or 'v'")
    return ImageOp("flip", (axis,))


def crop_op(left: float, top: float, right: float, bottom: float) -> ImageOp:
    """Crop box as fractions (0..1) of the current image width/height."""
    box = tuple(min(1.0, max(0.0, float(v))) for v in (left, top, right, bottom))
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        raise ValueError("The crop area is empty.")
    return ImageOp("crop", box)


def resize_op(mode: str, a: float, b: float | None = None) -> ImageOp:
    """mode 'exact' (width, height), 'fit' (max width, max height) or 'percent' (p)."""
    if mode == "percent":
        if not (1 <= a <= 1000):
            raise ValueError("Percentage must be between 1 and 1000.")
        return ImageOp("resize", ("percent", float(a), None))
    if mode in ("exact", "fit"):
        if not a or not b or a < 1 or b < 1 or a > 30000 or b > 30000:
            raise ValueError("Width and height must be between 1 and 30000 pixels.")
        return ImageOp("resize", (mode, int(a), int(b)))
    raise ValueError(f"unknown resize mode {mode}")


@dataclass
class ImageEdits:
    ops: list[ImageOp] = field(default_factory=list)
    brightness: float = 1.0
    contrast: float = 1.0

    @property
    def is_identity(self) -> bool:
        return not self.ops and self.brightness == 1.0 and self.contrast == 1.0

    def copy(self) -> ImageEdits:
        return replace(self, ops=list(self.ops))

    def describe(self) -> str:
        parts = [op.describe() for op in self.ops]
        if self.brightness != 1.0:
            parts.append(f"Brightness {self.brightness:.0%}")
        if self.contrast != 1.0:
            parts.append(f"Contrast {self.contrast:.0%}")
        return ", ".join(parts) if parts else "No changes"


def _resize_target(size: tuple[int, int], op: ImageOp) -> tuple[int, int]:
    mode, a, b = op.value
    width, height = size
    if mode == "percent":
        return max(1, round(width * a / 100)), max(1, round(height * a / 100))
    if mode == "fit":
        ratio = min(a / width, b / height)
        return max(1, round(width * ratio)), max(1, round(height * ratio))
    return int(a), int(b)


def _crop_box(size: tuple[int, int], op: ImageOp) -> tuple[int, int, int, int]:
    width, height = size
    left, top, right, bottom = op.value
    box = [round(left * width), round(top * height), round(right * width), round(bottom * height)]
    box[2] = max(box[2], box[0] + 1)
    box[3] = max(box[3], box[1] + 1)
    return box[0], box[1], min(box[2], width), min(box[3], height)


def size_after(size: tuple[int, int], ops: list[ImageOp]) -> tuple[int, int]:
    """Final pixel size after applying ``ops`` to an image of ``size``."""
    width, height = size
    for op in ops:
        if op.kind == "rotate" and op.value[0] in (90, 270):
            width, height = height, width
        elif op.kind == "crop":
            left, top, right, bottom = _crop_box((width, height), op)
            width, height = right - left, bottom - top
        elif op.kind == "resize":
            width, height = _resize_target((width, height), op)
    return width, height


def apply_edits(img: Image.Image, edits: ImageEdits, full_size: tuple[int, int] | None = None) -> Image.Image:
    """Apply ``edits``. ``img`` may be a downscaled preview of an image whose
    real size is ``full_size``; resize steps are then scaled accordingly."""
    current_full = full_size or img.size
    scale = img.width / current_full[0] if current_full[0] else 1.0
    for op in edits.ops:
        if op.kind == "rotate":
            method = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
            img = img.transpose(method[op.value[0]])
        elif op.kind == "flip":
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT if op.value[0] == "h" else Image.Transpose.FLIP_TOP_BOTTOM)
        elif op.kind == "crop":
            img = img.crop(_crop_box(img.size, op))
        elif op.kind == "resize":
            target_full = _resize_target(current_full, op)
            target = (max(1, round(target_full[0] * scale)), max(1, round(target_full[1] * scale)))
            if target != img.size:
                img = img.resize(target, Image.Resampling.LANCZOS)
        current_full = size_after(current_full, [op])
    if edits.brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(edits.brightness)
    if edits.contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(edits.contrast)
    return img


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
@dataclass
class LoadedImage:
    image: Image.Image
    full_size: tuple[int, int]  # size after EXIF orientation, before any preview downscale
    format: str  # Pillow format name of the source, e.g. "JPEG"
    exif: bytes | None
    icc_profile: bytes | None
    frames: int = 1


def normalize_mode(img: Image.Image) -> Image.Image:
    """Bring unusual pixel formats into RGB/RGBA/L/LA so every edit and every
    output format works."""
    mode = img.mode
    if mode in ("RGB", "RGBA", "L", "LA"):
        return img
    if mode in ("P", "PA"):
        has_alpha = mode == "PA" or "transparency" in img.info
        return img.convert("RGBA" if has_alpha else "RGB")
    if mode.startswith("I;16") or mode in ("I", "F"):
        # 16/32-bit grayscale: scale down to 8 bit instead of clipping to white.
        as_int = img.convert("I") if mode.startswith("I;16") else img
        high = 65535 if mode.startswith("I;16") or (as_int.getextrema()[1] or 0) > 255 else 255
        return as_int.point(lambda v: v * (255 / high)).convert("L")
    if mode == "1":
        return img.convert("L")
    if "A" in img.getbands():
        return img.convert("RGBA")
    return img.convert("RGB")


def _oriented_size(img: Image.Image) -> tuple[int, int]:
    try:
        orientation = img.getexif().get(_EXIF_ORIENTATION, 1)
    except Exception:  # noqa: BLE001 - broken EXIF must not stop loading
        orientation = 1
    width, height = img.size
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def load_image(path: Path, max_size: int | None = None) -> LoadedImage:
    """Open an image with EXIF orientation applied. With ``max_size`` a
    reduced-size copy is returned quickly (for previews)."""
    path = Path(path)
    try:
        with Image.open(path) as src:
            full_size = _oriented_size(src)
            fmt = src.format or ""
            info = dict(src.info)
            frames = getattr(src, "n_frames", 1) or 1
            if max_size and fmt == "JPEG":
                src.draft("RGB", (max_size, max_size))  # fast reduced-resolution JPEG decode
            src.load()
            img = ImageOps.exif_transpose(src)
            img = normalize_mode(img)
            if max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            exif = img.info.get("exif") or info.get("exif")
    except READ_ERRORS as exc:
        raise InvalidInputError(f"'{path.name}' could not be opened as an image.", details=str(exc)) from exc
    if exif:
        exif = _exif_without_orientation(exif)
    return LoadedImage(img, full_size, fmt, exif, info.get("icc_profile"), frames)


def _exif_without_orientation(exif_bytes: bytes) -> bytes | None:
    """Pixels are already rotated upright, so the orientation tag must be reset
    or viewers would rotate the image a second time."""
    try:
        exif = Image.Exif()
        exif.load(exif_bytes)
        if _EXIF_ORIENTATION in exif:
            exif[_EXIF_ORIENTATION] = 1
        return exif.tobytes()
    except Exception:  # noqa: BLE001 - drop unreadable EXIF rather than fail
        return None


def image_date_taken(path: Path) -> float | None:
    """EXIF 'date taken' as a timestamp (None if absent)."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get_ifd(_EXIF_IFD).get(_EXIF_DATETIME_ORIGINAL) or exif.get(_EXIF_DATETIME)
    except READ_ERRORS:
        return None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return dt.datetime.strptime(str(raw).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def image_summary(path: Path) -> str:
    path = Path(path)
    try:
        with Image.open(path) as img:
            width, height = _oriented_size(img)
            lines = [
                f"File:        {path.name}",
                f"Format:      {img.format or '-'}",
                f"Size:        {width} x {height} pixels",
                f"Color mode:  {img.mode}",
                f"File size:   {human_size(path.stat().st_size)}",
            ]
            dpi = img.info.get("dpi")
            if dpi:
                lines.append(f"Resolution:  {round(float(dpi[0]))} DPI")
            frames = getattr(img, "n_frames", 1)
            if frames and frames > 1:
                lines.append(f"Frames:      {frames} (only the first frame is edited)")
    except READ_ERRORS as exc:
        return f"{path.name}: could not be read ({exc})"
    taken = image_date_taken(path)
    if taken:
        lines.append(f"Date taken:  {dt.datetime.fromtimestamp(taken):%Y-%m-%d %H:%M}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Saving
# ----------------------------------------------------------------------------
def output_extension(source: Path, format_key: str) -> str:
    if format_key == "keep":
        ext = source.suffix.lower().lstrip(".")
        return ext if ext in PIL_FORMATS else "png"
    return format_key


def _flatten(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    if img.mode in ("RGBA", "LA"):
        base = Image.new("RGB", img.size, background)
        base.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
        return base
    return img if img.mode in ("RGB", "L") else img.convert("RGB")


def save_image(
    img: Image.Image,
    path: Path,
    quality: int = 90,
    exif: bytes | None = None,
    icc_profile: bytes | None = None,
) -> None:
    """Save in the format given by the file extension, written to a temporary
    file first and renamed when complete."""
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    fmt = PIL_FORMATS.get(ext)
    if fmt is None:
        raise InvalidInputError(f"Saving images as '.{ext}' is not supported.")
    quality = int(min(100, max(1, quality)))
    params: dict = {}
    if fmt == "JPEG":
        img = _flatten(img)
        params = {"quality": quality, "optimize": True}
    elif fmt == "PNG":
        params = {"compress_level": 6}
    elif fmt == "WEBP":
        params = {"quality": quality, "method": 4, "lossless": quality >= 100}
    elif fmt == "BMP":
        img = _flatten(img)
    elif fmt == "TIFF":
        params = {"compression": "tiff_lzw"}
    elif fmt == "GIF":
        if img.mode not in ("RGB", "RGBA", "L", "P"):
            img = img.convert("RGBA")
    elif fmt == "ICO":
        img = img.convert("RGBA")
        sizes = [s for s in ICO_SIZES if s <= max(img.size)] or [min(ICO_SIZES)]
        params = {"sizes": [(s, s) for s in sizes]}
    metadata = {}
    if fmt in ("JPEG", "WEBP", "PNG", "TIFF"):
        if exif:
            metadata["exif"] = exif
        if icc_profile:
            metadata["icc_profile"] = icc_profile

    tmp = temp_sibling(path)
    try:
        try:
            img.save(tmp, fmt, **params, **metadata)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if not metadata:
                raise
            log.info("Saving %s with metadata failed (%s); saving without it", path.name, exc)
            img.save(tmp, fmt, **params)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class ImageTask:
    source: Path
    edits: ImageEdits


def process_images(
    tasks: list[ImageTask],
    output_dir: Path,
    format_key: str,
    quality: int,
    name_suffix: str,
    keep_metadata: bool,
    ctx: JobContext,
) -> JobResult:
    """Apply each task's edits and save the results into ``output_dir``.
    Files that fail are reported, never silently dropped."""
    if not tasks:
        raise InvalidInputError("There are no images to save.")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    planned = [
        output_dir / build_filename(f"{t.source.stem}{name_suffix}", output_extension(t.source, format_key))
        for t in tasks
    ]
    planned = ctx.resolve_outputs(planned)
    outputs: list[Path] = []
    failures: list[str] = []
    ctx.set_status(f"Saving {len(tasks)} image(s)...")
    for number, (task, target) in enumerate(zip(tasks, planned, strict=True)):
        ctx.check_cancelled()
        ctx.step(number, len(tasks), f"Image {number + 1} / {len(tasks)}: {task.source.name}")
        try:
            loaded = load_image(task.source)
            result = apply_edits(loaded.image, task.edits)
            save_image(
                result,
                target,
                quality=quality,
                exif=loaded.exif if keep_metadata else None,
                icc_profile=loaded.icc_profile if keep_metadata else None,
            )
            outputs.append(target)
        except InvalidInputError as exc:
            failures.append(f"{task.source.name}: {exc.message}")
        except READ_ERRORS as exc:
            log.warning("Saving %s failed", task.source, exc_info=True)
            failures.append(f"{task.source.name}: {exc}")
    ctx.step(len(tasks), len(tasks))
    if not outputs:
        raise ProcessingError("None of the images could be saved.", details="\n".join(failures))
    result = JobResult(f"Saved {len(outputs)} image(s) to {output_dir}", outputs=outputs)
    if failures:
        result.warnings.append(f"{len(failures)} image(s) could not be saved:\n" + "\n".join(failures))
    return result


def rename_file(path: Path, new_stem: str) -> Path:
    """Rename a file on disk, keeping its extension."""
    path = Path(path)
    if not path.exists():
        raise InvalidInputError(f"The file no longer exists:\n{path}")
    target = path.with_name(build_filename(new_stem, path.suffix))
    if target.name == path.name:
        return path
    if target.exists() and not os.path.samefile(target, path):
        raise InvalidInputError(f"A file named '{target.name}' already exists in this folder.")
    path.rename(target)  # also handles case-only renames on Windows
    log.info("Renamed %s -> %s", path.name, target.name)
    return target
