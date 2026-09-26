"""Data passed between the translation provider, the page processors and the job."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Kinds of text the vision model reports on image pages.
REGION_KINDS = ("dialogue", "thought", "narration", "caption", "sign", "sfx", "text", "other")


@dataclass
class TextSegment:
    """One block of extractable PDF text (usually a paragraph)."""

    id: str
    text: str
    rect: tuple[float, float, float, float]  # PDF points (x0, y0, x1, y1)
    font_size: float = 11.0
    color: str = "#000000"
    bold: bool = False
    serif: bool = False


@dataclass
class Region:
    """One piece of text found on an image page by the vision model."""

    box: tuple[float, float, float, float]  # normalised 0..1 (x0, y0, x1, y1) of the page
    kind: str
    original: str
    translation: str

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Region:
        return cls(tuple(data["box"]), data.get("kind", "text"), data.get("original", ""), data.get("translation", ""))


@dataclass
class PagePlan:
    """How one page of the input is processed."""

    index: int  # 0-based page index in the source
    mode: str  # "text", "image" or "skip"
    segments: list[TextSegment] = field(default_factory=list)
    chars: int = 0
