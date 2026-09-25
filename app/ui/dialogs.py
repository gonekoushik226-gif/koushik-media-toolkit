"""Message boxes used across the application."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox, QWidget

from app.config import paths
from app.config.settings import OVERWRITE_RENAME, OVERWRITE_REPLACE
from app.core.errors import ErrorReport, describe_exception
from app.utils.filenames import unique_path, unique_paths
from app.utils.system import open_path


def show_error(parent: QWidget | None, report: ErrorReport | BaseException) -> None:
    if isinstance(report, BaseException):
        report = describe_exception(report)
    box = QMessageBox(QMessageBox.Icon.Critical, report.title, report.message, QMessageBox.StandardButton.Ok, parent)
    if report.details:
        box.setDetailedText(report.details)
    if report.unexpected:
        box.setInformativeText("Technical details were written to the log file.")
    logs = box.addButton("Open log folder", QMessageBox.ButtonRole.ActionRole)
    box.exec()
    if box.clickedButton() is logs:
        open_path(paths.log_dir())


def show_warning(parent: QWidget | None, title: str, message: str, details: str = "") -> None:
    box = QMessageBox(QMessageBox.Icon.Warning, title, message, QMessageBox.StandardButton.Ok, parent)
    if details:
        box.setInformativeText(details if len(details) < 600 else details[:600] + "...")
        box.setDetailedText(details)
    box.exec()


def show_info(parent: QWidget | None, title: str, message: str) -> None:
    QMessageBox.information(parent, title, message)


def confirm(parent: QWidget | None, title: str, message: str, yes_text: str = "Yes", no_text: str = "Cancel") -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, message, QMessageBox.StandardButton.NoButton, parent)
    yes = box.addButton(yes_text, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(no_text, QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is yes


def resolve_conflicts(parent: QWidget | None, planned: list[Path], existing: list[Path], policy: str) -> list[Path] | None:
    """Apply the overwrite policy to output files that already exist.
    Returns the final paths, or None if the user cancelled."""
    if not existing:
        return list(planned)
    if policy == OVERWRITE_REPLACE:
        return list(planned)
    if policy == OVERWRITE_RENAME:
        return unique_paths(list(planned))
    folder = existing[0].parent
    if len(existing) == 1:
        title = "File already exists"
        text = f"'{existing[0].name}' already exists in\n{folder}\n\nDo you want to replace it?"
    else:
        names = "\n".join(p.name for p in existing[:8]) + ("\n..." if len(existing) > 8 else "")
        title = "Files already exist"
        text = f"{len(existing)} of the files already exist in\n{folder}\n\n{names}\n\nDo you want to replace them?"
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.NoButton, parent)
    replace = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton("Keep both", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.exec()
    clicked = box.clickedButton()
    if clicked is replace:
        return list(planned)
    if clicked is keep:
        return unique_paths(list(planned))
    return None


def confirm_output(parent: QWidget | None, path: Path, policy: str) -> Path | None:
    """Single-file version of :func:`resolve_conflicts`."""
    if not path.exists():
        return path
    if policy == OVERWRITE_RENAME:
        return unique_path(path)
    result = resolve_conflicts(parent, [path], [path], policy)
    return result[0] if result else None
