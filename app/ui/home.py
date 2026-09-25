"""Home screen: the four main modules as cards plus utility buttons."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from app import APP_NAME, __version__
from app.core.registry import GROUP_MAIN, GROUP_UTILITY, ModuleRegistry
from app.ui import icons, theme


class ModuleCard(QPushButton):
    """A large clickable card (keyboard accessible, since it is a button)."""

    def __init__(self, pixmap: QPixmap, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("moduleCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(260, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName(title)
        self.setToolTip(text)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(pixmap)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        body = QLabel(text)
        body.setObjectName("cardText")
        body.setWordWrap(True)
        for widget in (icon, heading, body):
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(widget)
        layout.addStretch(1)


class HomePage(QWidget):
    def __init__(self, ctx, registry: ModuleRegistry, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        accent = theme.colors()["accent"]
        text_color = theme.colors()["text"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 32, 48, 28)
        layout.setSpacing(18)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("Everyday video, audio, image and PDF tasks in one place.")
        subtitle.setObjectName("appSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        grid = QGridLayout()
        grid.setSpacing(18)
        for index, spec in enumerate(registry.specs(GROUP_MAIN)):
            card = ModuleCard(icons.pixmap(spec.icon, accent, 40), spec.title, spec.description)
            card.clicked.connect(lambda _=False, key=spec.key: ctx.navigate(key))
            grid.addWidget(card, index // 2, index % 2)
        layout.addLayout(grid, 1)

        row = QHBoxLayout()
        row.setSpacing(12)
        for spec in registry.specs(GROUP_UTILITY):
            button = QPushButton(f"  {spec.title}")
            button.setObjectName("utilityButton")
            button.setIcon(icons.icon(spec.icon, text_color, 20))
            button.setToolTip(spec.description)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, key=spec.key: ctx.navigate(key))
            row.addWidget(button)
        row.addStretch(1)
        version = QLabel(f"Version {__version__}")
        version.setObjectName("muted")
        row.addWidget(version)
        layout.addLayout(row)
