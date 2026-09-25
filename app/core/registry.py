"""Registry of the tool modules shown on the home screen.

This is the main extension point: a new module (e.g. "Subtitles") is added
by writing a page widget and registering one :class:`ModuleSpec`. The home
screen and navigation are built from the registry, so nothing else has to
change. See docs/DEVELOPER_GUIDE.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

GROUP_MAIN = "main"  # large cards on the home screen
GROUP_UTILITY = "utility"  # smaller buttons below the cards


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    title: str
    description: str
    icon: str  # name of an SVG in assets/icons (without extension)
    factory: Callable[[Any], Any]  # (AppContext) -> QWidget page
    group: str = GROUP_MAIN
    order: int = 100


class ModuleRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ModuleSpec] = {}

    def register(self, spec: ModuleSpec) -> None:
        if spec.key in self._specs:
            raise ValueError(f"A module with key '{spec.key}' is already registered")
        self._specs[spec.key] = spec

    def get(self, key: str) -> ModuleSpec:
        return self._specs[key]

    def __contains__(self, key: str) -> bool:
        return key in self._specs

    def specs(self, group: str | None = None) -> list[ModuleSpec]:
        items = [s for s in self._specs.values() if group is None or s.group == group]
        return sorted(items, key=lambda s: (s.order, s.title))
