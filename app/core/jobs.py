"""Progress reporting and cancellation for background operations.

Service functions receive a :class:`JobContext`. They report progress with
``set_status``/``set_progress`` and call ``check_cancelled()`` at safe points.
Long-running subprocesses register a cancel callback so Cancel can stop them
immediately. The UI layer connects the callbacks to Qt signals; tests simply
use a plain ``JobContext()``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.core.errors import JobCancelled
from app.utils.filenames import unique_paths

log = logging.getLogger(__name__)

T = TypeVar("T")
StatusCallback = Callable[[str], None]
ProgressCallback = Callable[[float | None, str], None]
# (planned paths, the ones that already exist) -> final paths, or None to cancel
ConflictResolver = Callable[[list[Path], list[Path]], "list[Path] | None"]


class JobContext:
    """Thread-safe progress/cancel channel between a job and whoever runs it."""

    def __init__(
        self,
        on_status: StatusCallback | None = None,
        on_progress: ProgressCallback | None = None,
        min_interval: float = 0.1,
        resolver: ConflictResolver | None = None,
    ):
        self._on_status = on_status
        self._on_progress = on_progress
        self._min_interval = min_interval
        self._resolver = resolver
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._last_emit = 0.0
        self._last_fraction: float | None = None
        self.status = ""
        self.fraction: float | None = None
        self.detail = ""

    # -- reporting ---------------------------------------------------------
    def set_status(self, text: str) -> None:
        self.status = text
        if self._on_status:
            self._on_status(text)

    def set_progress(self, fraction: float | None, detail: str = "") -> None:
        """``fraction`` in 0..1, or ``None`` for "busy, amount unknown"."""
        if fraction is not None:
            fraction = min(1.0, max(0.0, float(fraction)))
        now = time.monotonic()
        is_edge = fraction in (0.0, 1.0) or (fraction is None) != (self._last_fraction is None)
        if not is_edge and detail == self.detail and now - self._last_emit < self._min_interval:
            return  # throttle chatty sources (yt-dlp calls hooks per chunk)
        self._last_emit = now
        self._last_fraction = fraction
        self.fraction = fraction
        self.detail = detail
        if self._on_progress:
            self._on_progress(fraction, detail)

    def step(self, done: int, total: int, detail: str | None = None) -> None:
        """Convenience for countable work: ``Page 3 / 10``."""
        self.set_progress(done / total if total else None, detail if detail is not None else f"{done} / {total}")

    # -- output conflicts --------------------------------------------------
    def resolve_outputs(self, paths: list[Path]) -> list[Path]:
        """Decide what happens to planned output files that already exist.

        Used by operations that create several files or only learn the final
        name while running. The UI installs a resolver that follows the
        user's overwrite setting (possibly asking); without one, existing
        files are never overwritten - new files get a number instead.
        Raises JobCancelled if the user chose Cancel.
        """
        existing = [p for p in paths if p.exists()]
        if not existing:
            return list(paths)
        if self._resolver is None:
            return unique_paths(list(paths))
        final = self._resolver(list(paths), existing)
        if final is None:
            raise JobCancelled()
        return final

    # -- cancellation ------------------------------------------------------
    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise JobCancelled()

    def cancel(self) -> None:
        """Request cancellation (safe to call from any thread, repeatedly)."""
        if self._cancel_event.is_set():
            return
        self._cancel_event.set()
        with self._lock:
            callbacks = list(self._cancel_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a failing callback must not break cancel
                log.debug("cancel callback failed", exc_info=True)

    def add_cancel_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register ``callback``; returns a function that unregisters it. If the
        job is already cancelled the callback runs immediately."""
        with self._lock:
            self._cancel_callbacks.append(callback)
        if self._cancel_event.is_set():
            callback()

        def remove() -> None:
            with self._lock:
                if callback in self._cancel_callbacks:
                    self._cancel_callbacks.remove(callback)

        return remove

    def wait(self, seconds: float) -> None:
        """Sleep that wakes up (and raises) on cancel."""
        if self._cancel_event.wait(seconds):
            raise JobCancelled()


def run_abandonable(func: Callable[[], T], ctx: JobContext, poll: float = 0.1) -> T:
    """Run ``func`` (which cannot be interrupted, e.g. a library call without
    hooks) in a helper thread. If the user cancels, stop waiting immediately
    and let the helper finish in the background; its result is discarded."""
    box: dict[str, object] = {}
    finished = threading.Event()

    def target() -> None:
        try:
            box["result"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=target, name="abandonable", daemon=True)
    thread.start()
    while not finished.wait(poll):
        ctx.check_cancelled()
    ctx.check_cancelled()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]
