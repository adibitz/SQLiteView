"""Ctrl+K command palette: fuzzy search across actions and database objects."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget


@dataclass
class PaletteEntry:
    title: str
    subtitle: str
    icon: QIcon
    run: Callable[[], None]
    shortcut: str = ""


def fuzzy_score(query: str, text: str) -> Optional[int]:
    """Subsequence match; lower is better. Returns None when ``query`` doesn't match."""
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    if q in t:
        return t.index(q)
    pos = -1
    score = 0
    for ch in q:
        nxt = t.find(ch, pos + 1)
        if nxt < 0:
            return None
        score += (nxt - pos - 1) * 3
        pos = nxt
    return 100 + score


class CommandPalette(QFrame):
    def __init__(self, parent: QWidget, provider: Callable[[], list[PaletteEntry]]):
        super().__init__(parent)
        self.provider = provider
        self.entries: list[PaletteEntry] = []
        self.setProperty("role", "palette")
        self.setWindowFlags(Qt.Widget)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        from PySide6.QtGui import QColor
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a command, table or column…")
        self.list = QListWidget()
        self.list.setIconSize(QSize(18, 18))
        self.list.setUniformItemSizes(True)
        lay.addWidget(self.edit)
        lay.addWidget(self.list)
        self.edit.textChanged.connect(self._refilter)
        self.edit.installEventFilter(self)
        self.list.itemActivated.connect(self._run_item)
        self.list.itemClicked.connect(self._run_item)
        parent.installEventFilter(self)
        self.hide()

    def open(self, initial: str = "") -> None:
        self.entries = self.provider()
        p = self.parentWidget()
        w = min(640, p.width() - 80)
        self.setGeometry((p.width() - w) // 2, 70, w, min(440, p.height() - 120))
        self.edit.setText(initial)
        self._refilter()
        self.show()
        self.raise_()
        self.edit.setFocus()
        self.edit.selectAll()

    def _refilter(self) -> None:
        q = self.edit.text().strip()
        scored = []
        for i, e in enumerate(self.entries):
            s = fuzzy_score(q, e.title)
            if s is None:
                s2 = fuzzy_score(q, e.subtitle)
                if s2 is None:
                    continue
                s = 500 + s2
            scored.append((s, i, e))
        scored.sort(key=lambda x: (x[0], x[1]))
        self.list.clear()
        for _, _, e in scored[:200]:
            text = e.title if not e.subtitle else f"{e.title}    —  {e.subtitle}"
            if e.shortcut:
                text += f"    [{e.shortcut}]"
            it = QListWidgetItem(e.icon, text)
            it.setData(Qt.UserRole, e)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _run_item(self, item: QListWidgetItem) -> None:
        e: PaletteEntry = item.data(Qt.UserRole)
        self.hide()
        if e is not None:
            e.run()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.parentWidget() and ev.type() == QEvent.Resize and self.isVisible():
            self.open(self.edit.text())
            return False
        if obj is self.edit and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k == Qt.Key_Escape:
                self.hide()
                return True
            if k in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp):
                row = self.list.currentRow() + {Qt.Key_Down: 1, Qt.Key_Up: -1, Qt.Key_PageDown: 8, Qt.Key_PageUp: -8}[k]
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                it = self.list.currentItem()
                if it is not None:
                    self._run_item(it)
                return True
        if obj is self.edit and ev.type() == QEvent.FocusOut and not self.list.hasFocus():
            # Close when clicking elsewhere
            from PySide6.QtCore import QTimer
            QTimer.singleShot(120, lambda: (not self.list.hasFocus() and not self.edit.hasFocus()) and self.hide())
        return super().eventFilter(obj, ev)
