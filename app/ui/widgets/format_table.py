"""Table of downloadable formats reported by yt-dlp."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from app.models.remote import FormatKind, RemoteFormat, friendly_codec
from app.ui import theme
from app.utils.units import human_bitrate, human_size

VIDEO_COLUMNS = ["Type", "Resolution", "FPS", "Video codec", "Audio codec", "Ext", "Size", "Bitrate", "Note", "ID"]
AUDIO_COLUMNS = ["Codec", "Bitrate", "Sample rate", "Channels", "Ext", "Size", "Language", "Note", "ID"]
BEST = "best"
FORMAT_ROLE = Qt.ItemDataRole.UserRole


def _channels(count: int | None) -> str:
    return {None: "-", 1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(count, f"{count} ch")


def _size(fmt: RemoteFormat) -> str:
    size = fmt.size_bytes
    if size is None:
        return "-"
    return f"~{human_size(size)}" if fmt.size_is_estimate else human_size(size)


def _note(fmt: RemoteFormat) -> str:
    parts = [fmt.format_note] if fmt.format_note else []
    if fmt.dynamic_range and fmt.dynamic_range != "SDR":
        parts.append(fmt.dynamic_range)
    if fmt.is_manifest:
        parts.append("streaming")
    if fmt.kind_is_guess:
        parts.append("details not reported by the site")
    return ", ".join(parts)


class FormatTable(QTableWidget):
    selection_changed = Signal(object)  # RemoteFormat, or None for "best available"

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.mode = mode
        columns = VIDEO_COLUMNS if mode == "video" else AUDIO_COLUMNS
        self.setColumnCount(len(columns))
        self.setHorizontalHeaderLabels(columns)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setWordWrap(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(columns.index("Note"), QHeaderView.ResizeMode.Stretch)
        self.setMinimumHeight(240)
        self.itemSelectionChanged.connect(lambda: self.selection_changed.emit(self.current_format()))

    def set_formats(self, formats: list[RemoteFormat], best_text: str) -> None:
        self.blockSignals(True)
        self.clearContents()
        self.setRowCount(len(formats) + 1)
        best = QTableWidgetItem(f"★  Best available (automatic) - {best_text}")
        best.setData(FORMAT_ROLE, BEST)
        font = best.font()
        font.setBold(True)
        best.setFont(font)
        self.setItem(0, 0, best)
        self.setSpan(0, 0, 1, self.columnCount())
        for row, fmt in enumerate(formats, start=1):
            for column, text in enumerate(self._cells(fmt)):
                item = QTableWidgetItem(text)
                item.setData(FORMAT_ROLE, fmt)
                if column == 0 and self.mode == "video":
                    item.setForeground(QBrush(QColor(self._kind_color(fmt.kind))))
                    item.setToolTip(self._kind_tip(fmt))
                elif self.mode == "video" and column in (3, 4):
                    item.setToolTip((fmt.vcodec if column == 3 else fmt.acodec) or "")
                elif self.mode == "audio" and column == 0:
                    item.setToolTip(fmt.acodec or "")
                self.setItem(row, column, item)
        self.blockSignals(False)

    def _cells(self, fmt: RemoteFormat) -> list[str]:
        if self.mode == "video":
            return [
                fmt.kind.label + (" ?" if fmt.kind_is_guess else ""),
                fmt.resolution_text,
                f"{fmt.fps:g}" if fmt.fps else "-",
                friendly_codec(fmt.vcodec) if fmt.has_video else "-",
                friendly_codec(fmt.acodec) if fmt.acodec else ("none" if fmt.kind is FormatKind.VIDEO_ONLY else "-"),
                fmt.ext,
                _size(fmt),
                human_bitrate(fmt.total_bitrate),
                _note(fmt),
                fmt.format_id,
            ]
        note = _note(fmt)
        if fmt.kind is FormatKind.COMBINED:
            note = "video with sound - the audio will be extracted" + (f", {note}" if note else "")
        return [
            friendly_codec(fmt.acodec) if fmt.acodec else "-",
            human_bitrate(fmt.audio_bitrate or (fmt.abr if fmt.kind is FormatKind.COMBINED else None)),
            f"{fmt.asr / 1000:g} kHz" if fmt.asr else "-",
            _channels(fmt.audio_channels),
            fmt.ext,
            _size(fmt),
            fmt.language or "-",
            note,
            fmt.format_id,
        ]

    @staticmethod
    def _kind_color(kind: FormatKind) -> str:
        c = theme.colors()
        return {FormatKind.COMBINED: c["success"], FormatKind.VIDEO_ONLY: c["accent"], FormatKind.AUDIO_ONLY: c["warning"]}[kind]

    @staticmethod
    def _kind_tip(fmt: RemoteFormat) -> str:
        if fmt.kind is FormatKind.VIDEO_ONLY:
            return "Picture only. The best matching audio is added automatically (see the options below)."
        if fmt.kind is FormatKind.AUDIO_ONLY:
            return "Sound only."
        return "Picture and sound in one file."

    def current_format(self):
        items = self.selectedItems()
        if not items:
            return None if self.rowCount() == 0 else BEST
        value = items[0].data(FORMAT_ROLE)
        return value

    def select_best(self) -> None:
        if self.rowCount():
            self.selectRow(0)

    def select_format(self, fmt: RemoteFormat) -> None:
        for row in range(1, self.rowCount()):
            item = self.item(row, 0)
            if item is not None and item.data(FORMAT_ROLE) is fmt:
                self.selectRow(row)
                self.scrollToItem(item)
                return
        self.select_best()
