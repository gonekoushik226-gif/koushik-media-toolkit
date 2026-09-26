"""DIAGNOSTICS page."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from app import APP_NAME, __version__
from app.config import paths
from app.services.diagnostics import ERROR, INFO, OK, WARNING, Check, run_diagnostics
from app.ui import theme
from app.ui.jobs import run_in_background
from app.ui.widgets.common import label
from app.utils.system import open_path

SYMBOLS = {OK: "✓", WARNING: "⚠", ERROR: "✗", INFO: "ℹ"}
WORDS = {OK: "Available", WARNING: "Warning", ERROR: "Missing / problem", INFO: "Info"}


class DiagnosticsPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._checks: list[Check] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        title = QLabel("Diagnostics")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(label("Checks that everything the application needs is present and working. "
                               "Select a line to see details and how to fix a problem.", "muted"))
        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run all checks (including internet)")
        self.run_button.clicked.connect(lambda: self.run_checks(True))
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        logs = QPushButton("Open log folder")
        logs.clicked.connect(lambda: open_path(paths.log_dir()))
        buttons.addWidget(self.run_button)
        buttons.addWidget(copy)
        buttons.addWidget(logs)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Status", "Check", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(self._show_details)
        layout.addWidget(self.tree, 1)
        self.details = label("", "")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.details.setMinimumHeight(110)
        layout.addWidget(self.details)
        self._ran = False

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self._ran:
            self._ran = True
            self.run_checks(False)

    def run_checks(self, include_network: bool) -> None:
        self.run_button.setEnabled(False)
        self.tree.clear()
        self.details.setText("Checking..." + (" (testing the internet connection can take a few seconds)" if include_network else ""))
        settings, locator = self.ctx.settings, self.ctx.tools
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        qt = f"PySide6 {pyside_version}, Qt {qVersion()}"

        def done(checks: list[Check]) -> None:
            self.run_button.setEnabled(True)
            self._show(checks)

        def failed(exc: BaseException) -> None:
            self.run_button.setEnabled(True)
            self.details.setText(f"The checks could not be completed: {exc}")

        try:
            key_status = self.ctx.key_manager().status()  # masked; never the whole key
        except Exception:  # noqa: BLE001 - diagnostics must still run
            key_status = "Could not read the API key status"
        run_in_background(lambda: run_diagnostics(settings, locator, include_network, qt, key_status), done, failed,
                          owner=self)

    def _show(self, checks: list[Check]) -> None:
        self._checks = checks
        colors = theme.colors()
        color = {OK: colors["success"], WARNING: colors["warning"], ERROR: colors["danger"], INFO: colors["muted"]}
        for check in checks:
            item = QTreeWidgetItem([f"{SYMBOLS[check.status]}  {WORDS[check.status]}", check.name, check.summary])
            item.setForeground(0, QBrush(QColor(color[check.status])))
            item.setData(0, Qt.ItemDataRole.UserRole, check)
            self.tree.addTopLevelItem(item)
        problems = sum(1 for c in checks if c.status == ERROR)
        warnings = sum(1 for c in checks if c.status == WARNING)
        if problems:
            self.details.setText(f"{problems} problem(s) found. Select a line marked ✗ to see how to fix it.")
        elif warnings:
            self.details.setText(f"Everything required is available. {warnings} warning(s) - select them for details.")
        else:
            self.details.setText("Everything is available and working.")

    def _show_details(self, item, _previous=None) -> None:
        if item is None:
            return
        check: Check = item.data(0, Qt.ItemDataRole.UserRole)
        text = f"{check.name}: {check.summary}"
        if check.details:
            text += f"\n\n{check.details}"
        if check.fix:
            text += f"\n\nHow to fix: {check.fix}"
        self.details.setText(text)

    def _copy(self) -> None:
        lines = [f"{APP_NAME} {__version__} - diagnostics report"]
        for check in self._checks:
            lines.append(f"[{WORDS[check.status]}] {check.name}: {check.summary}")
            if check.details:
                lines.extend(f"    {line}" for line in check.details.splitlines())
            if check.fix:
                lines.append(f"    Fix: {check.fix}")
        QGuiApplication.clipboard().setText("\n".join(lines))
