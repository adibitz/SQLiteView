"""Row details: every column of one record shown as a vertical form, with previous/next navigation."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
                               QScrollArea, QVBoxLayout, QWidget)

from ...core.data import image_format, is_binary, value_to_text
from ...core.rowexport import format_rows
from ...core.table_model import SqliteTableModel
from ..theme import mono_font
from ..widgets import label, tool_button


class RecordDialog(QDialog):
    def __init__(self, model: SqliteTableModel, row: int, first_column: int, parent=None):
        super().__init__(parent)
        self.model = model
        self.row = row
        self.first_column = first_column  # 1 when column 0 is the hidden rowid
        self.setWindowTitle("Row Details")
        self.resize(640, 680)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 12)
        lay.setSpacing(10)
        head = QHBoxLayout()
        self.title = label("", "h2")
        head.addWidget(self.title, 1)
        self.btn_prev = tool_button("arrow-up", "Previous row (Ctrl+Up)", on_click=lambda: self.go(-1))
        self.btn_next = tool_button("arrow-down", "Next row (Ctrl+Down)", on_click=lambda: self.go(1))
        head.addWidget(self.btn_prev)
        head.addWidget(self.btn_next)
        head.addWidget(tool_button("copy", "Copy row as JSON", on_click=self.copy_json))
        lay.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        lay.addWidget(self.scroll, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        QShortcut(QKeySequence("Ctrl+Up"), self, lambda: self.go(-1))
        QShortcut(QKeySequence("Ctrl+Down"), self, lambda: self.go(1))
        self._fill()

    def _columns(self) -> range:
        return range(self.first_column, self.model.columnCount())

    def _fill(self) -> None:
        m = self.model
        total = m.total_rows() if hasattr(m, "total_rows") else m.rowCount()
        absolute = (m.row_offset() if hasattr(m, "row_offset") else 0) + self.row + 1
        self.title.setText(f"Row {absolute:,} of {total:,}")
        self.btn_prev.setEnabled(self.row > 0)
        self.btn_next.setEnabled(self.row < m.rowCount() - 1)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        values = m.row_values(self.row) or []
        for r, c in enumerate(self._columns()):
            name = label(m.column_name(c), "muted")
            name.setAlignment(Qt.AlignRight | Qt.AlignTop)
            grid.addWidget(name, r, 0)
            v = values[c] if c < len(values) else None
            grid.addWidget(self._value_widget(v), r, 1)
        grid.setRowStretch(len(self._columns()), 1)
        self.scroll.setWidget(body)

    def _value_widget(self, v) -> QWidget:
        if v is None:
            w = label(self.model.null_text, "faint")
            return w
        if isinstance(v, (bytes, bytearray)) and is_binary(v):
            fmt = image_format(v)
            if fmt:
                from PySide6.QtGui import QPixmap
                pm = QPixmap()
                if pm.loadFromData(bytes(v)):
                    img = QLabel()
                    img.setPixmap(pm if pm.height() <= 160 else pm.scaledToHeight(160, Qt.SmoothTransformation))
                    img.setToolTip(f"{fmt} image · {len(v):,} bytes")
                    return img
            return label(f"BLOB · {len(v):,} bytes", "faint")
        text = value_to_text(v)
        if "\n" in text or len(text) > 80:
            ed = QPlainTextEdit(text)
            ed.setReadOnly(True)
            ed.setFont(mono_font())
            ed.setMaximumHeight(160)
            return ed
        lb = QLabel(text)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lb.setWordWrap(True)
        return lb

    def go(self, delta: int) -> None:
        r = self.row + delta
        if 0 <= r < self.model.rowCount():
            if not self.model.row_available(r):
                self.model.complete_cache()
            self.row = r
            self._fill()

    def copy_json(self) -> None:
        values = self.model.row_values(self.row) or []
        names = [self.model.column_name(c) for c in self._columns()]
        QGuiApplication.clipboard().setText(format_rows("json", names, [[values[c] for c in self._columns()]]))
