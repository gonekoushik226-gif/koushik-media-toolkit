"""The main window: header with Home button, page stack, status area."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from app import APP_NAME
from app.config.paths import asset_path
from app.core.errors import AppError
from app.core.registry import ModuleRegistry
from app.ui import icons, theme
from app.ui.dialogs import confirm, show_error
from app.ui.home import HomePage
from app.ui.jobs import JobController
from app.ui.widgets.status_area import StatusArea

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, ctx, registry: ModuleRegistry):
        super().__init__()
        self.ctx = ctx
        self.registry = registry
        ctx.window = self
        ctx.navigate = self.navigate
        self.setWindowTitle(APP_NAME)
        icon_path = asset_path("app.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(1000, 680)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        self.header = header
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 6, 16, 6)
        self.home_button = QPushButton("  Home")
        self.home_button.setObjectName("navButton")
        self.home_button.setToolTip("Back to the home screen (Ctrl+H)")
        self.home_button.clicked.connect(lambda: self.navigate("home"))
        self.page_title = QLabel(APP_NAME)
        self.page_title.setObjectName("pageTitle")
        header_layout.addWidget(self.home_button)
        header_layout.addSpacing(8)
        header_layout.addWidget(self.page_title)
        header_layout.addStretch(1)
        layout.addWidget(header)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.status = StatusArea()
        layout.addWidget(self.status)
        self.setCentralWidget(central)

        ctx.jobs = JobController(self, self.status, lambda: ctx.settings)
        self.home = HomePage(ctx, registry)
        self.stack.addWidget(self.home)
        self._pages: dict[str, QWidget] = {}
        self._refresh_header_icons()
        self.navigate("home")

        QShortcut(QKeySequence("Ctrl+H"), self, activated=lambda: self.navigate("home"))
        QShortcut(QKeySequence("Alt+Home"), self, activated=lambda: self.navigate("home"))
        self._restore_geometry()

    def _refresh_header_icons(self) -> None:
        self.home_button.setIcon(icons.icon("home", theme.colors()["text"], 20))

    def navigate(self, key: str, operation: str | None = None) -> None:
        if key == "home":
            self.stack.setCurrentWidget(self.home)
            self.header.setVisible(False)
            self.setWindowTitle(APP_NAME)
            return
        if key not in self.registry:
            log.warning("Unknown module %s", key)
            return
        spec = self.registry.get(key)
        page = self._pages.get(key)
        if page is None:
            try:
                page = spec.factory(self.ctx)
            except AppError as exc:
                show_error(self, exc)
                return
            self._pages[key] = page
            self.stack.addWidget(page)
        if operation and hasattr(page, "show_operation"):
            page.show_operation(operation)
        self.stack.setCurrentWidget(page)
        self.page_title.setText(spec.title)
        self.header.setVisible(True)
        self.setWindowTitle(f"{spec.title.title()} - {APP_NAME}")

    def current_page(self) -> QWidget:
        return self.stack.currentWidget()

    def page(self, key: str) -> QWidget | None:
        return self._pages.get(key)

    # -- window state --------------------------------------------------------
    def _restore_geometry(self) -> None:
        saved = self.ctx.settings.window_geometry
        if saved:
            try:
                if self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii"))):
                    return
            except (ValueError, TypeError):
                pass
        self.resize(1280, 820)

    def closeEvent(self, event):  # noqa: N802 - Qt API
        jobs = self.ctx.jobs
        if jobs is not None and jobs.busy:
            if not confirm(self, "Operation running",
                           "An operation is still running. Cancel it and close the application?", "Cancel and close",
                           "Keep running"):
                event.ignore()
                return
        if jobs is not None:
            jobs.shutdown()
        self.ctx.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.ctx.save_settings()
        log.info("Application closed")
        event.accept()

