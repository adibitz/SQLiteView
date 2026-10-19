"""SQL log panel (port of upstream's "SQL Log" dock)."""
from __future__ import annotations

import time

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from ..core.db import LogMessageType
from .sql_editor import SqlHighlighter
from .theme import mono_font
from .widgets import label, tool_button


class LogPanel(QWidget):
    SOURCES = [("Submitted by the user", LogMessageType.User), ("Application", LogMessageType.App),
               ("Error log", LogMessageType.ErrorLog)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._logs: dict[int, list[str]] = {int(t): [] for _, t in self.SOURCES}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(label("Show", "muted"))
        self.source = QComboBox()
        for text, _ in self.SOURCES:
            self.source.addItem(text)
        self.source.setCurrentIndex(1)
        self.source.currentIndexChanged.connect(self._refresh)
        head.addWidget(self.source)
        head.addStretch(1)
        head.addWidget(tool_button("trash", "Clear log", on_click=self.clear))
        lay.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(mono_font())
        self.view.setMaximumBlockCount(5000)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.highlighter = SqlHighlighter(self.view.document())
        lay.addWidget(self.view, 1)

    def log(self, sql: str, msgtype: int) -> None:
        entry = f"-- {time.strftime('%H:%M:%S')}\n{sql}\n"
        buf = self._logs.setdefault(msgtype, [])
        buf.append(entry)
        if len(buf) > 5000:
            del buf[:1000]
        if self.SOURCES[self.source.currentIndex()][1] == msgtype:
            self.view.appendPlainText(entry)

    def _refresh(self) -> None:
        t = int(self.SOURCES[self.source.currentIndex()][1])
        self.view.setPlainText("\n".join(self._logs.get(t, [])[-2000:]))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def clear(self) -> None:
        t = int(self.SOURCES[self.source.currentIndex()][1])
        self._logs[t] = []
        self.view.clear()
