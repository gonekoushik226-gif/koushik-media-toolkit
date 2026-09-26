"""Light and dark themes (Fusion style + palette + a small style sheet)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from app.config.paths import asset_path

LIGHT = {
    "window": "#f3f5f8", "surface": "#ffffff", "surface_alt": "#eef1f5", "border": "#d6dbe3",
    "text": "#1c2230", "muted": "#5b6577", "accent": "#2563eb", "accent_hover": "#1d4ed8",
    "accent_text": "#ffffff", "danger": "#c62828", "success": "#2e7d32", "warning": "#a15c00",
    "card_hover": "#e8f0ff", "selection": "#d6e4ff", "selection_text": "#0f1a33", "disabled": "#9aa3b1",
}
DARK = {
    "window": "#16181d", "surface": "#1e2128", "surface_alt": "#262a33", "border": "#353b47",
    "text": "#e6e9ef", "muted": "#9aa3b3", "accent": "#5b8cff", "accent_hover": "#7ba3ff",
    "accent_text": "#0b1020", "danger": "#ef5350", "success": "#66bb6a", "warning": "#ffb74d",
    "card_hover": "#232f47", "selection": "#2b3c62", "selection_text": "#ffffff", "disabled": "#646c7a",
}

_current: dict[str, str] = LIGHT


def colors() -> dict[str, str]:
    return _current


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # very old Qt
        return False


def resolve(theme: str) -> str:
    if theme == "system":
        return "dark" if system_prefers_dark() else "light"
    return "dark" if theme == "dark" else "light"


def _palette(c: dict[str, str]) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: c["window"],
        QPalette.ColorRole.WindowText: c["text"],
        QPalette.ColorRole.Base: c["surface"],
        QPalette.ColorRole.AlternateBase: c["surface_alt"],
        QPalette.ColorRole.Text: c["text"],
        QPalette.ColorRole.Button: c["surface"],
        QPalette.ColorRole.ButtonText: c["text"],
        QPalette.ColorRole.ToolTipBase: c["surface"],
        QPalette.ColorRole.ToolTipText: c["text"],
        QPalette.ColorRole.Highlight: c["accent"],
        QPalette.ColorRole.HighlightedText: c["accent_text"],
        QPalette.ColorRole.PlaceholderText: c["muted"],
        QPalette.ColorRole.Link: c["accent"],
        QPalette.ColorRole.BrightText: c["danger"],
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["disabled"]))
    return palette


def _stylesheet(c: dict[str, str]) -> str:
    down = asset_path("icons", "chevron-down.svg").as_posix()
    up = asset_path("icons", "chevron-up.svg").as_posix()
    check = asset_path("icons", "check.svg").as_posix()
    return f"""
    QWidget {{ font-size: 10pt; }}
    QToolTip {{ color: {c['text']}; background: {c['surface']}; border: 1px solid {c['border']}; padding: 4px; }}
    QLabel#pageTitle {{ font-size: 17pt; font-weight: 600; }}
    QLabel#appTitle {{ font-size: 24pt; font-weight: 700; }}
    QLabel#appSubtitle, QLabel#muted, QLabel#hint {{ color: {c['muted']}; }}
    QLabel#sectionTitle {{ font-size: 13pt; font-weight: 600; }}
    QLabel#warningText {{ color: {c['warning']}; }}
    QLabel#errorText {{ color: {c['danger']}; }}
    QLabel#successText {{ color: {c['success']}; }}
    QFrame#header {{ background: {c['surface']}; border-bottom: 1px solid {c['border']}; }}
    QFrame#updateBanner {{ background: {c['card_hover']}; border-bottom: 1px solid {c['border']}; }}
    QFrame#updateBanner QLabel {{ background: transparent; }}
    QFrame#statusArea {{ background: {c['surface']}; border-top: 1px solid {c['border']}; }}
    QFrame#panelCard {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QGroupBox {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 8px;
                 margin-top: 14px; padding: 12px 10px 10px 10px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['muted']}; font-weight: 600; }}
    QPushButton {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 6px 14px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:pressed {{ background: {c['surface_alt']}; }}
    QPushButton:disabled {{ color: {c['disabled']}; border-color: {c['border']}; }}
    QPushButton[primary="true"] {{ background: {c['accent']}; color: {c['accent_text']}; border: none;
                                   font-weight: 600; padding: 8px 22px; }}
    QPushButton[primary="true"]:hover {{ background: {c['accent_hover']}; }}
    QPushButton[primary="true"]:disabled {{ background: {c['border']}; color: {c['disabled']}; }}
    QPushButton#moduleCard {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 14px;
                              padding: 18px; text-align: left; }}
    QPushButton#moduleCard:hover {{ background: {c['card_hover']}; border-color: {c['accent']}; }}
    QPushButton#utilityButton {{ border-radius: 10px; padding: 10px 18px; }}
    QPushButton#navButton {{ border: none; background: transparent; padding: 6px 10px; font-weight: 600; }}
    QPushButton#navButton:hover {{ background: {c['surface_alt']}; }}
    QLabel#cardTitle {{ font-size: 15pt; font-weight: 700; }}
    QLabel#cardText {{ color: {c['muted']}; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 5px 7px;
        selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; border: 1px solid {c['muted']};
                                                     background: {c['surface']}; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
    QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; image: url("{check}"); }}
    QRadioButton::indicator:checked {{ border: 5px solid {c['accent']}; background: {c['surface']}; width: 7px; height: 7px; }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {c['border']}; background: {c['surface_alt']}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 24px; border: none; }}
    QComboBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
    QAbstractSpinBox {{ padding-right: 22px; }}
    QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none;
                                  margin-top: 2px; }}
    QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px;
                                    border: none; margin-bottom: 2px; }}
    QAbstractSpinBox::up-arrow {{ image: url("{up}"); width: 10px; height: 10px; }}
    QAbstractSpinBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
    QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled, QComboBox::down-arrow:disabled {{ image: none; }}
    QComboBox QAbstractItemView {{ background: {c['surface']}; selection-background-color: {c['selection']};
                                   selection-color: {c['selection_text']}; }}
    QListWidget, QTableWidget, QTreeWidget {{ background: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 8px; alternate-background-color: {c['surface_alt']}; }}
    QListWidget::item {{ padding: 4px; }}
    QListWidget::item:selected, QTableWidget::item:selected, QTreeWidget::item:selected {{
        background: {c['selection']}; color: {c['selection_text']}; }}
    QListWidget#operationList {{ background: transparent; border: none; font-size: 10.5pt; outline: 0; }}
    QListWidget#operationList::item {{ padding: 9px 12px; border-radius: 7px; margin: 1px 4px; }}
    QListWidget#operationList::item:hover {{ background: {c['surface_alt']}; }}
    QListWidget#operationList::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
    QHeaderView::section {{ background: {c['surface_alt']}; border: none; border-bottom: 1px solid {c['border']};
                            padding: 5px 6px; font-weight: 600; }}
    QProgressBar {{ background: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 6px;
                    text-align: center; min-height: 18px; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; background: {c['surface']}; top: -1px; }}
    QTabBar::tab {{ padding: 7px 14px; border: 1px solid transparent; border-bottom: none; }}
    QTabBar::tab:selected {{ background: {c['surface']}; border-color: {c['border']}; border-top-left-radius: 6px;
                             border-top-right-radius: 6px; font-weight: 600; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget#scrollContent {{ background: transparent; }}
    QSplitter::handle {{ background: {c['window']}; }}
    """


def apply_theme(app: QApplication, theme: str) -> str:
    """Apply 'system', 'light' or 'dark'; returns the resolved name."""
    global _current
    resolved = resolve(theme)
    _current = DARK if resolved == "dark" else LIGHT
    app.setStyle("Fusion")
    app.setPalette(_palette(_current))
    app.setStyleSheet(_stylesheet(_current))
    return resolved
