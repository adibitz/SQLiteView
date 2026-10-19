"""Small dialogs."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..widgets import label

SHORTCUTS = [
    ("General", [("Command palette", "Ctrl+K"), ("Open database", "Ctrl+O"), ("New database", "Ctrl+N"),
                 ("Open read-only", "Ctrl+Shift+O"), ("Write changes", "Ctrl+S"), ("Database info & pragmas", "Ctrl+I"),
                 ("Preferences", "Ctrl+,"), ("Toggle light/dark", "Ctrl+Shift+L"), ("Close tab", "Ctrl+W"),
                 ("New SQL editor", "Ctrl+T"), ("Create table", "Ctrl+Shift+T")]),
    ("SQL editor", [("Execute all / selection", "Ctrl+Enter, F5, Ctrl+R"), ("Execute current statement", "Shift+F5"),
                    ("Execute current line", "Shift+F6"), ("Autocomplete", "Ctrl+Space"), ("Toggle comment", "Ctrl+/"),
                    ("Find / replace", "Ctrl+F"), ("Indent / outdent", "Tab / Shift+Tab")]),
    ("Data grid", [("Copy", "Ctrl+C"), ("Copy with headers", "Ctrl+Shift+C"), ("Paste", "Ctrl+V"), ("Set to NULL", "Delete"),
                   ("Edit cell", "F2 or type"), ("Edit in inspector", "Ctrl+Enter"), ("Refresh", "F5"),
                   ("Sort by several columns", "Ctrl+click header"), ("Follow foreign key", "Ctrl+Shift+click")]),
    ("Inspector", [("Apply changes", "Ctrl+Enter")]),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(520, 600)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.addWidget(label("Keyboard Shortcuts", "title"))
        rows = []
        for section, items in SHORTCUTS:
            rows.append((section, None))
            rows.extend(items)
        t = QTableWidget(len(rows), 2)
        t.horizontalHeader().hide()
        t.verticalHeader().hide()
        t.setShowGrid(False)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionMode(QTableWidget.NoSelection)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for r, (a, b) in enumerate(rows):
            ia = QTableWidgetItem(a)
            if b is None:
                f = ia.font()
                f.setBold(True)
                ia.setFont(f)
                t.setSpan(r, 0, 1, 2)
            else:
                ib = QTableWidgetItem(b)
                ib.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(r, 1, ib)
            t.setItem(r, 0, ia)
        lay.addWidget(t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
