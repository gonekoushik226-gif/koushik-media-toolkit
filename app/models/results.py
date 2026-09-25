"""The value every background operation returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class JobResult:
    message: str  # one line, e.g. "Saved clip.mp4"
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # shown to the user after success
    details: str = ""  # optional longer text (e.g. media information)
    data: Any = None  # structured result for the UI (e.g. analysed formats)

    @property
    def output_dir(self) -> Path | None:
        return self.outputs[0].parent if self.outputs else None
