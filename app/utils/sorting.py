"""Sorting helpers for file lists (natural order, dates, sizes, shuffle)."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path

_DIGITS = re.compile(r"(\d+)")


class SortKey(StrEnum):
    NAME = "name"
    NATURAL = "natural"
    DATE_TAKEN = "date_taken"
    CREATED = "created"
    MODIFIED = "modified"
    SIZE = "size"

    @property
    def label(self) -> str:
        return {
            SortKey.NAME: "Name (A-Z)",
            SortKey.NATURAL: "Name, natural (2 before 10)",
            SortKey.DATE_TAKEN: "Date taken (photo EXIF)",
            SortKey.CREATED: "Date created",
            SortKey.MODIFIED: "Date modified",
            SortKey.SIZE: "File size",
        }[self]


def natural_key(text: str) -> tuple:
    """Key so that 'page2' sorts before 'page10'.

    ``re.split`` with a capture group always alternates text/number, so the
    lists only ever compare str with str and int with int.
    """
    folded = text.casefold()
    parts = [int(tok) if i % 2 else tok for i, tok in enumerate(_DIGITS.split(folded))]
    return (parts, folded, text)


def _name_of(path: Path) -> str:
    return path.name


def created_time(path: Path) -> float:
    stat = path.stat()
    # st_birthtime is the real creation time on Windows (Python 3.12+).
    return float(getattr(stat, "st_birthtime", stat.st_ctime))


def _safe(getter: Callable[[Path], float | None]) -> Callable[[Path], float]:
    def wrapped(path: Path) -> float:
        try:
            value = getter(path)
        except OSError:
            value = None
        return float("inf") if value is None else float(value)

    return wrapped


def sort_paths(
    paths: Iterable[Path],
    key: SortKey,
    reverse: bool = False,
    date_taken: Callable[[Path], float | None] | None = None,
) -> list[Path]:
    """Return a new sorted list. Ties are broken by natural file name so the
    result is deterministic. Files whose date/size cannot be read go last."""
    items = [Path(p) for p in paths]
    items.sort(key=lambda p: natural_key(_name_of(p)))  # stable tie-breaker first
    if key is SortKey.NAME:
        items.sort(key=lambda p: p.name.casefold(), reverse=reverse)
        return items
    if key is SortKey.NATURAL:
        items.sort(key=lambda p: natural_key(_name_of(p)), reverse=reverse)
        return items

    if key is SortKey.CREATED:
        getter = _safe(created_time)
    elif key is SortKey.MODIFIED:
        getter = _safe(lambda p: p.stat().st_mtime)
    elif key is SortKey.SIZE:
        getter = _safe(lambda p: p.stat().st_size)
    elif key is SortKey.DATE_TAKEN:
        # Photos without EXIF dates fall back to their creation time.
        def taken_or_created(p: Path) -> float | None:
            value = date_taken(p) if date_taken else None
            return value if value is not None else created_time(p)

        getter = _safe(taken_or_created)
    else:  # pragma: no cover - exhaustive enum
        raise ValueError(f"Unknown sort key: {key}")

    decorated = [(getter(p), p) for p in items]  # each file is read only once
    known = [(k, p) for k, p in decorated if k != float("inf")]
    unknown = [p for k, p in decorated if k == float("inf")]
    known.sort(key=lambda pair: pair[0], reverse=reverse)
    return [p for _, p in known] + unknown


def shuffled(paths: Iterable[Path], rng: random.Random | None = None) -> list[Path]:
    items = list(paths)
    (rng or random.Random()).shuffle(items)
    return items


def move_items(items: list, indices: Iterable[int], offset: int) -> tuple[list, list[int]]:
    """Move the items at ``indices`` up (offset=-1) or down (offset=+1) by one
    step as a block. Returns the new list and the new indices. Items already at
    the edge stop the whole move, matching typical list-box behaviour."""
    selected = sorted(set(indices))
    if not selected or offset not in (-1, 1):
        return list(items), selected
    if (offset < 0 and selected[0] == 0) or (offset > 0 and selected[-1] == len(items) - 1):
        return list(items), selected
    result = list(items)
    order = selected if offset < 0 else list(reversed(selected))
    for index in order:
        result[index], result[index + offset] = result[index + offset], result[index]
    return result, [i + offset for i in selected]
