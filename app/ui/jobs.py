"""Running work off the GUI thread.

* :class:`JobController` runs one user-visible operation at a time on a
  QThread, shows it in the status area, supports Cancel, and turns results
  and errors into messages.
* :func:`run_in_background` is for small helper tasks (reading a file's
  length, loading a preview) that should not block the window but are not
  "operations" the user started.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal

from app.core.errors import JobCancelled, describe_exception
from app.core.jobs import JobContext
from app.models.results import JobResult
from app.utils.system import reveal_in_explorer

log = logging.getLogger(__name__)


class _ConflictRequest:
    def __init__(self, planned: list[Path], existing: list[Path]):
        self.planned = planned
        self.existing = existing
        self.result: list[Path] | None = None
        self.done = threading.Event()


class _JobThread(QThread):
    status = Signal(str)
    progress = Signal(object, str)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    conflict = Signal(object)

    def __init__(self, title: str, fn: Callable[[JobContext], JobResult], parent: QObject):
        super().__init__(parent)
        self.title = title
        self._fn = fn
        self.ctx = JobContext(on_status=self.status.emit, on_progress=self.progress.emit, resolver=self._resolve)

    def _resolve(self, planned: list[Path], existing: list[Path]) -> list[Path] | None:
        """Called on the worker thread; asks the GUI thread and waits."""
        request = _ConflictRequest(planned, existing)
        self.conflict.emit(request)
        while not request.done.wait(0.1):
            if self.ctx.is_cancelled:
                return None
        return request.result

    def run(self) -> None:  # worker thread
        try:
            result = self._fn(self.ctx)
        except JobCancelled:
            log.info("Job cancelled: %s", self.title)
            self.cancelled.emit()
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the UI
            if self.ctx.is_cancelled:
                log.info("Job cancelled: %s (%s)", self.title, type(exc).__name__)
                self.cancelled.emit()
                return
            report = describe_exception(exc)
            if report.unexpected:
                log.error("Job failed unexpectedly: %s", self.title, exc_info=exc)
            else:
                log.warning("Job failed: %s - %s | %s", self.title, report.message, report.details)
            self.failed.emit(exc)
        else:
            log.info("Job finished: %s - %s", self.title, getattr(result, "message", ""))
            self.succeeded.emit(result)


class JobController(QObject):
    busy_changed = Signal(bool)

    def __init__(self, window, status_area, settings_getter: Callable[[], object]):
        super().__init__(window)
        self._window = window
        self._status = status_area
        self._settings = settings_getter
        self._current: _JobThread | None = None
        self._threads: set[_JobThread] = set()
        self._callbacks: dict = {}
        status_area.cancel_requested.connect(self.cancel)

    @property
    def busy(self) -> bool:
        return self._current is not None

    def start(
        self,
        title: str,
        fn: Callable[[JobContext], JobResult],
        on_success: Callable[[JobResult], None] | None = None,
        on_failure: Callable[[BaseException], None] | None = None,
        reveal: bool | None = None,
    ) -> bool:
        """Run ``fn`` on a worker thread. ``reveal`` opens the output folder when
        done (None = use the setting)."""
        if self.busy:
            from app.ui.dialogs import show_info

            show_info(self._window, "Please wait", "Another operation is still running. "
                      "Wait for it to finish or press Cancel in the status bar.")
            return False
        log.info("Job started: %s", title)
        thread = _JobThread(title, fn, self)
        thread.status.connect(self._status.set_status)
        thread.progress.connect(self._status.set_progress)
        thread.succeeded.connect(self._on_success)
        thread.failed.connect(self._on_failure)
        thread.cancelled.connect(self._on_cancelled)
        thread.conflict.connect(self._on_conflict)
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        self._threads.add(thread)
        self._current = thread
        self._callbacks = {"success": on_success, "failure": on_failure, "reveal": reveal}
        self._status.start(title)
        self.busy_changed.emit(True)
        thread.start()
        return True

    def cancel(self) -> None:
        if self._current is not None:
            log.info("Cancel requested: %s", self._current.title)
            self._status.set_cancelling()
            self._current.ctx.cancel()

    def _finish(self) -> dict:
        callbacks = self._callbacks
        self._current = None
        self._callbacks = {}
        self.busy_changed.emit(False)
        return callbacks

    def _on_success(self, result: JobResult) -> None:
        callbacks = self._finish()
        outputs = list(getattr(result, "outputs", []) or [])
        self._status.finish_success(getattr(result, "message", "Done"), outputs)
        reveal = callbacks.get("reveal")
        if reveal is None:
            reveal = bool(getattr(self._settings(), "open_folder_after", False))
        if reveal and outputs:
            reveal_in_explorer(outputs[0])
        warnings = list(getattr(result, "warnings", []) or [])
        if warnings:
            from app.ui.dialogs import show_warning

            show_warning(self._window, "Finished with warnings", result.message, "\n\n".join(warnings))
        if callbacks.get("success"):
            callbacks["success"](result)

    def _on_failure(self, exc: BaseException) -> None:
        callbacks = self._finish()
        report = describe_exception(exc)
        self._status.finish_error(report.message.splitlines()[0])
        from app.ui.dialogs import show_error

        show_error(self._window, report)
        if callbacks.get("failure"):
            callbacks["failure"](exc)

    def _on_cancelled(self) -> None:
        self._finish()
        self._status.finish_cancelled()

    def _on_conflict(self, request: _ConflictRequest) -> None:
        from app.ui.dialogs import resolve_conflicts

        try:
            policy = getattr(self._settings(), "overwrite_policy", "ask")
            request.result = resolve_conflicts(self._window, request.planned, request.existing, policy)
        finally:
            request.done.set()

    def _on_thread_finished(self, thread: _JobThread) -> None:
        self._threads.discard(thread)
        thread.deleteLater()

    def shutdown(self, timeout_ms: int = 8000) -> None:
        """Cancel the running job and wait briefly (used when the window closes)."""
        for thread in list(self._threads):
            thread.ctx.cancel()
            thread.wait(timeout_ms)
        QThreadPool.globalInstance().waitForDone(3000)  # small helper tasks (previews, checks)


# ----------------------------------------------------------------------------
# Small background helpers
# ----------------------------------------------------------------------------
class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self.fn()
        except BaseException as exc:  # noqa: BLE001
            self._emit(self.signals.failed, exc)
        else:
            self._emit(self.signals.done, result)

    @staticmethod
    def _emit(signal, value) -> None:
        try:
            signal.emit(value)
        except RuntimeError:
            pass  # the application is shutting down; nobody is waiting for the result


_active_tasks: set[_Task] = set()


def run_in_background(
    fn: Callable[[], object],
    on_done: Callable[[object], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    owner: QObject | None = None,
) -> None:
    """Run ``fn`` on the thread pool; callbacks run on the GUI thread and are
    skipped if ``owner`` (a widget) was destroyed in the meantime."""
    task = _Task(fn)
    task.setAutoDelete(False)
    _active_tasks.add(task)

    def alive() -> bool:
        return owner is None or shiboken6.isValid(owner)

    def done(result: object) -> None:
        _active_tasks.discard(task)
        if on_done and alive():
            on_done(result)

    def failed(exc: BaseException) -> None:
        _active_tasks.discard(task)
        log.debug("Background task failed: %s", exc, exc_info=exc)
        if on_error and alive():
            on_error(exc)

    task.signals.done.connect(done)
    task.signals.failed.connect(failed)
    QThreadPool.globalInstance().start(task)
