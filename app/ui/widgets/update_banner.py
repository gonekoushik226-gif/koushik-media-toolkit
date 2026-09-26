"""A slim bar at the top of the window announcing a new version."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from app import APP_NAME, __version__
from app.services.updates import RELEASE_PAGE_PREFIX, UpdateInfo
from app.ui.widgets.common import primary_button, set_bold


class UpdateBanner(QFrame):
    skipped = Signal(str)  # version the user does not want to hear about again

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self.info: UpdateInfo | None = None
        self.text = QLabel("")
        self.text.setWordWrap(True)
        set_bold(self.text)
        self.download = primary_button("Download...")
        self.download.setMinimumHeight(30)
        self.download.setToolTip("Open the release page to download the new version")
        self.download.clicked.connect(self.open_release_page)
        self.skip = QPushButton("Skip this version")
        self.skip.clicked.connect(self._skip)
        self.later = QPushButton("Later")
        self.later.setToolTip("Hide this message until the app is started again")
        self.later.clicked.connect(self.hide)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.addWidget(self.text, 1)
        for button in (self.download, self.skip, self.later):
            layout.addWidget(button)
        self.hide()

    def show_update(self, info: UpdateInfo) -> None:
        self.info = info
        released = f", released {info.published}" if info.published else ""
        self.text.setText(f"{APP_NAME} {info.version} is available{released} - you have {__version__}.")
        self.download.setToolTip(f"Open the page of version {info.version}. Download the installer and run it to "
                                 "update - your settings are kept.")
        self.show()

    def open_release_page(self) -> bool:
        if self.info is None or not self.info.page_url.startswith(RELEASE_PAGE_PREFIX):
            return False
        return QDesktopServices.openUrl(QUrl(self.info.page_url))

    def _skip(self) -> None:
        if self.info is not None:
            self.skipped.emit(self.info.version)
        self.hide()
