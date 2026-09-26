"""Removing original text from page images (speech bubbles, boxes, signs).

The AI reports a box around each piece of text. From there:

1. If the text sits on a light (or dark) background, flood-fill from inside
   the box to find the enclosing speech bubble / caption box. If that region
   is closed (it does not run off into the whole page), the letters inside it
   are "holes" in the region; bubble + holes are painted in the bubble's own
   colour, and the bubble's interior becomes the space for the translation.
2. Otherwise (text on artwork, or an open background) the text box itself is
   covered with the colour found just around it.

Only the painted pixels are returned (as a transparent patch), so the rest of
the artwork is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

LIGHT = 185  # grey level from which a pixel counts as "bubble white"
DARK = 70  # ... and up to which it counts as "box black"
MIN_BACKGROUND_SHARE = 0.45


@dataclass
class CleanPlan:
    text_box: tuple[int, int, int, int]  # where the translation goes (pixels)
    background: tuple[int, int, int]
    foreground: tuple[int, int, int]
    patch: Image.Image | None = None  # RGBA; transparent where nothing changes
    patch_box: tuple[int, int, int, int] | None = None
    bubble: bool = False
    label: bool = False  # sound effect: keep the artwork, show a small label instead


def luminance(color: tuple[int, int, int]) -> float:
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _foreground_for(background: tuple[int, int, int]) -> tuple[int, int, int]:
    return (0, 0, 0) if luminance(background) >= 128 else (255, 255, 255)


def _clamp_box(box, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, x1 = max(0, min(x0, width - 1)), max(1, min(x1, width))
    y0, y1 = max(0, min(y0, height - 1)), max(1, min(y1, height))
    if x1 <= x0:
        x1 = min(width, x0 + 1)
    if y1 <= y0:
        y1 = min(height, y0 + 1)
    return x0, y0, x1, y1


def _median(image: Image.Image, mask: Image.Image | None = None) -> tuple[int, int, int]:
    stat = ImageStat.Stat(image.convert("RGB"), mask=mask)
    return tuple(int(v) for v in stat.median[:3])


def _background_polarity(image: Image.Image, box) -> str | None:
    gray = image.crop(box).convert("L")
    histogram = gray.histogram()
    total = max(1, sum(histogram))
    if sum(histogram[LIGHT:]) / total >= MIN_BACKGROUND_SHARE:
        return "light"
    if sum(histogram[: DARK + 1]) / total >= MIN_BACKGROUND_SHARE:
        return "dark"
    return None


def _seeds(binary: Image.Image, box):
    """Background pixels to start the flood fill from (relative coordinates):
    first just outside the text box (where the bubble's own background is),
    then inside it from the centre outwards."""
    x0, y0, x1, y1 = box
    width, height = binary.size
    pixels = binary.load()
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    gap = 4
    candidates = [(cx, y0 - gap), (cx, y1 + gap), (x0 - gap, cy), (x1 + gap, cy),
                  (x0 - gap, y0 - gap), (x1 + gap, y0 - gap), (x0 - gap, y1 + gap), (x1 + gap, y1 + gap)]
    steps = 12
    for ring in range(steps + 1):
        fx = (x1 - x0) / 2 * ring / steps
        fy = (y1 - y0) / 2 * ring / steps
        for dx, dy in ((0, 0), (-fx, 0), (fx, 0), (0, -fy), (0, fy), (-fx, -fy), (fx, fy), (-fx, fy), (fx, -fy)):
            candidates.append((int(cx + dx), int(cy + dy)))
    for x, y in candidates:
        if 0 <= x < width and 0 <= y < height and pixels[x, y] == 255:
            yield x, y


def _surrounds(bbox, box, share: float = 0.75) -> bool:
    """Does the region's bounding box cover most of the text box? (A letter's
    enclosed counter - the hole in an "A" or "O" - does not.)"""
    width, height = max(1, box[2] - box[0]), max(1, box[3] - box[1])
    cover_x = min(bbox[2], box[2]) - max(bbox[0], box[0])
    cover_y = min(bbox[3], box[3]) - max(bbox[1], box[1])
    return cover_x >= share * width and cover_y >= share * height


def _flood(image: Image.Image, crop_box, box, polarity: str):
    """Flood-fill the background around the text inside ``crop_box``.
    Returns (crop, region mask, region bbox, area, open sides, image sides) or None."""
    x0, y0, x1, y1 = box
    crop = image.crop(crop_box)
    gray = crop.convert("L")
    binary = gray.point(lambda v: 255 if v >= LIGHT else 0) if polarity == "light" else \
        gray.point(lambda v: 255 if v <= DARK else 0)
    rel_box = (x0 - crop_box[0], y0 - crop_box[1], x1 - crop_box[0], y1 - crop_box[1])
    tried = Image.new("L", crop.size, 0)  # pixels of regions already rejected
    attempts = 0
    for seed in _seeds(binary, rel_box):
        if tried.getpixel(seed):
            continue
        attempts += 1
        if attempts > 12:
            return None
        filled = binary.copy()
        ImageDraw.floodfill(filled, seed, 128, thresh=0)
        region = filled.point(lambda v: 255 if v == 128 else 0)
        bbox = region.getbbox()
        if bbox is not None and _surrounds(bbox, rel_box):
            break
        tried = ImageChops.lighter(tried, region)
    else:
        return None
    cw, ch = crop.size
    width, height = image.size
    sides = (bbox[0] <= 1, bbox[1] <= 1, bbox[2] >= cw - 1, bbox[3] >= ch - 1)
    at_image_edge = (crop_box[0] == 0, crop_box[1] == 0, crop_box[2] == width, crop_box[3] == height)
    open_sides = sum(touch and not edge for touch, edge in zip(sides, at_image_edge, strict=True))
    image_sides = sum(touch and edge for touch, edge in zip(sides, at_image_edge, strict=True))
    return crop, region, bbox, region.histogram()[255], open_sides, image_sides


def _bubble_plan(image: Image.Image, box, polarity: str) -> CleanPlan | None:
    width, height = image.size
    x0, y0, x1, y1 = box
    # Start with a crop a little larger than the text and grow it until the
    # background region is closed (a bubble) - or give up (open page / artwork).
    for factor in (1.0, 2.5, 5.0):
        margin_x, margin_y = max(int((x1 - x0) * factor), 30), max(int((y1 - y0) * factor), 30)
        crop_box = (max(0, x0 - margin_x), max(0, y0 - margin_y), min(width, x1 + margin_x), min(height, y1 + margin_y))
        found = _flood(image, crop_box, box, polarity)
        if found is None:
            return None
        crop, region, bbox, area, open_sides, image_sides = found
        if not open_sides:
            break
    else:
        return None  # the "bubble" runs into the open page: not a closed bubble
    if image_sides >= 2 or area > 0.5 * width * height:
        return None  # page background (e.g. a scanned book page), not a bubble
    cw, ch = crop.size
    rel_box = (x0 - crop_box[0], y0 - crop_box[1], x1 - crop_box[0], y1 - crop_box[1])
    # Letters are holes in the region: everything not reachable from outside.
    outside = ImageOps.expand(region, border=1, fill=0)
    ImageDraw.floodfill(outside, (0, 0), 64, thresh=0)
    holes = outside.crop((1, 1, cw + 1, ch + 1)).point(lambda v: 255 if v == 0 else 0)
    fill = ImageChops.lighter(region, holes)
    # Letters touching the bubble outline are not holes; cover the text box too (inside the bubble).
    inner = (max(rel_box[0], bbox[0] + 3), max(rel_box[1], bbox[1] + 3), min(rel_box[2], bbox[2] - 3),
             min(rel_box[3], bbox[3] - 3))
    if inner[2] > inner[0] and inner[3] > inner[1]:
        ImageDraw.Draw(fill).rectangle((inner[0], inner[1], inner[2] - 1, inner[3] - 1), fill=255)
    fill = fill.filter(ImageFilter.MaxFilter(3))  # also cover the anti-aliased edges of the letters
    background = _median(crop, region)
    patch = Image.composite(Image.new("RGBA", crop.size, (*background, 255)), Image.new("RGBA", crop.size, (0, 0, 0, 0)),
                            fill)
    fill_box = fill.getbbox() or (0, 0, cw, ch)
    patch = patch.crop(fill_box)
    patch_box = (crop_box[0] + fill_box[0], crop_box[1] + fill_box[1], crop_box[0] + fill_box[2],
                 crop_box[1] + fill_box[3])
    # Space for the translation: the bubble interior (inscribed area for round bubbles).
    bx0, by0, bx1, by1 = (crop_box[0] + bbox[0], crop_box[1] + bbox[1], crop_box[0] + bbox[2], crop_box[1] + bbox[3])
    roundness = area / max(1, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    shrink = 0.15 if roundness < 0.9 else 0.06
    sx, sy = (bx1 - bx0) * shrink, (by1 - by0) * shrink
    text_box = (int(min(bx0 + sx, x0)), int(min(by0 + sy, y0)), int(max(bx1 - sx, x1)), int(max(by1 - sy, y1)))
    text_box = (max(text_box[0], bx0 + 2), max(text_box[1], by0 + 2), min(text_box[2], bx1 - 2), min(text_box[3], by1 - 2))
    return CleanPlan(text_box=text_box, background=background, foreground=_foreground_for(background), patch=patch,
                     patch_box=patch_box, bubble=True)


def _box_plan(image: Image.Image, box) -> CleanPlan:
    width, height = image.size
    x0, y0, x1, y1 = box
    pad = max(3, int(0.08 * min(x1 - x0, y1 - y0)))
    rect = _clamp_box((x0 - pad, y0 - pad, x1 + pad, y1 + pad), width, height)
    ring_box = _clamp_box((rect[0] - 3, rect[1] - 3, rect[2] + 3, rect[3] + 3), width, height)
    ring_crop = image.crop(ring_box)
    ring_mask = Image.new("L", ring_crop.size, 255)
    inner = (rect[0] - ring_box[0], rect[1] - ring_box[1], rect[2] - ring_box[0] - 1, rect[3] - ring_box[1] - 1)
    ImageDraw.Draw(ring_mask).rectangle(inner, fill=0)
    if ring_mask.getbbox() is None:
        ring_mask = None  # the box covers the whole page
    background = _median(ring_crop, ring_mask)
    patch = Image.new("RGBA", (rect[2] - rect[0], rect[3] - rect[1]), (*background, 255))
    return CleanPlan(text_box=rect, background=background, foreground=_foreground_for(background), patch=patch,
                     patch_box=rect)


def plan_cleanup(image: Image.Image, box, kind: str, erase_sfx: bool = False) -> CleanPlan:
    """How to remove the text in ``box`` (pixel coordinates) and where to put the translation."""
    image = image.convert("RGB") if image.mode != "RGB" else image
    box = _clamp_box(box, *image.size)
    if kind == "sfx" and not erase_sfx:
        return CleanPlan(text_box=box, background=(255, 255, 255), foreground=(0, 0, 0), label=True)
    polarity = _background_polarity(image, box)
    if polarity is not None:
        plan = _bubble_plan(image, box, polarity)
        if plan is not None:
            return plan
    return _box_plan(image, box)
