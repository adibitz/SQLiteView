"""Database overview + pragma editor (replaces upstream's "Edit Pragmas" tab)."""
from __future__ import annotations

import os
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QMessageBox,
                               QScrollArea, QSpinBox, QDoubleSpinBox, QVBoxLayout, QWidget)

from ..core.data import human_readable_size
from ..core.db import JOURNAL_MODE_VALUES, LOCKING_MODE_VALUES, DBBrowserDB
from .widgets import label, push_button

# name, kind, choices/description
PRAGMAS: list[tuple[str, str, Any, str]] = [
    ("auto_vacuum", "enum", ["None", "Full", "Incremental"], "Reclaim free pages automatically"),
    ("automatic_index", "bool", None, "Let the planner create transient indices"),
    ("case_sensitive_like", "bool", None, "LIKE compares case-sensitively"),
    ("checkpoint_fullfsync", "bool", None, "F_FULLFSYNC during checkpoints (macOS)"),
    ("foreign_keys", "bool", None, "Enforce foreign key constraints"),
    ("fullfsync", "bool", None, "F_FULLFSYNC on commit (macOS)"),
    ("ignore_check_constraints", "bool", None, "Skip CHECK constraints"),
    ("journal_mode", "text", JOURNAL_MODE_VALUES, "Rollback journal mode"),
    ("journal_size_limit", "int", (-1, 2_147_483_647), "Max journal size after a transaction (bytes, -1 = none)"),
    ("locking_mode", "text", LOCKING_MODE_VALUES, "Database locking mode"),
    ("max_page_count", "int", (1, 4_294_967_294), "Maximum number of pages"),
    ("page_size", "pagesize", [512, 1024, 2048, 4096, 8192, 16384, 32768, 65536], "Page size in bytes (applies after VACUUM)"),
    ("recursive_triggers", "bool", None, "Allow triggers to fire recursively"),
    ("secure_delete", "bool", None, "Overwrite deleted content with zeros"),
    ("synchronous", "enum", ["Off", "Normal", "Full", "Extra"], "Disk synchronisation level"),
    ("temp_store", "enum", ["Default", "File", "Memory"], "Where temporary tables are stored"),
    ("user_version", "int", (-2_147_483_648, 2_147_483_647), "Application defined version number"),
    ("wal_autocheckpoint", "int", (0, 2_147_483_647), "WAL auto-checkpoint interval (pages)"),
]


class DbInfoTab(QWidget):
    kind = "info"
    statusMessage = Signal(str)
    pragmasSaved = Signal()

    def __init__(self, db: DBBrowserDB, parent=None):
        super().__init__(parent)
        self.db = db
        self._editors: dict[str, tuple[QWidget, Callable[[], Any], Callable[[Any], None]]] = {}
        self._values: dict[str, Any] = {}
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        body = QWidget()
        body.setProperty("role", "surface")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(14)
        lay.addWidget(label("Database Info", "title"))
        self.path_label = label("", "muted")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.path_label)

        self.stats = QGridLayout()
        self.stats.setHorizontalSpacing(12)
        self.stats.setVerticalSpacing(12)
        lay.addLayout(self.stats)

        card = QFrame()
        card.setProperty("role", "card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        head = QHBoxLayout()
        head.addWidget(label("Pragmas", "h2"))
        head.addStretch(1)
        self.btn_reset = push_button("Reset", kind="ghost", on_click=self.reload)
        self.btn_save = push_button("Save Pragmas", kind="primary", icon_name="check", on_click=self.save)
        head.addWidget(self.btn_reset)
        head.addWidget(self.btn_save)
        cl.addLayout(head)
        cl.addWidget(label("Changing pragmas commits pending changes. Some settings only persist for this connection.", "faint", wrap=True))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        for name, kind, choices, desc in PRAGMAS:
            w, getter, setter = self._make_editor(kind, choices)
            w.setToolTip(desc)
            row = QHBoxLayout()
            row.setSpacing(12)
            if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.setButtonSymbols(QSpinBox.NoButtons)
            row.addWidget(w)
            row.addWidget(label(desc, "faint"))
            row.addStretch(1)
            form.addRow(name.replace("_", " ").capitalize(), row)
            self._editors[name] = (w, getter, setter)
        cl.addLayout(form)
        lay.addWidget(card)
        lay.addStretch(1)

    @staticmethod
    def _make_editor(kind: str, choices):
        if kind == "bool":
            w = QCheckBox()
            return w, lambda: 1 if w.isChecked() else 0, lambda v: w.setChecked(bool(int(v or 0)))
        if kind in ("enum", "text", "pagesize"):
            w = QComboBox()
            w.addItems([str(c) for c in choices])
            w.setMinimumWidth(140)
            if kind == "enum":
                return w, w.currentIndex, lambda v: w.setCurrentIndex(int(v or 0))
            if kind == "text":
                return w, w.currentText, lambda v: w.setCurrentIndex(max(0, w.findText(str(v or "").upper())))
            return w, lambda: int(w.currentText()), lambda v: w.setCurrentIndex(max(0, w.findText(str(v))))
        lo, hi = choices
        if lo < -2**31 or hi > 2**31 - 1:
            # QSpinBox is 32-bit; use a double spin box without decimals for larger ranges
            w = QDoubleSpinBox()
            w.setDecimals(0)
        else:
            w = QSpinBox()
        w.setRange(lo, hi)
        w.setMinimumWidth(140)
        return w, lambda: int(w.value()), lambda v: w.setValue(int(v or 0))

    def _stat_card(self, row: int, col: int, title: str, value: str) -> None:
        card = QFrame()
        card.setProperty("role", "card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        cl.setSpacing(2)
        cl.addWidget(label(title.upper(), "section"))
        v = label(value, "h2")
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cl.addWidget(v)
        self.stats.addWidget(card, row, col)

    def reload(self) -> None:
        db = self.db
        if not db.is_open():
            return
        while self.stats.count():
            it = self.stats.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        path = db.cur_file
        self.path_label.setText(path if path != ":memory:" else "In-memory database")
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        page_size = db.get_pragma("page_size") or 0
        page_count = db.get_pragma("page_count") or 0
        freelist = db.get_pragma("freelist_count") or 0
        objmap = db.schemata.get("main")
        n_tables = sum(1 for k, t in objmap.tables.items() if not t.is_view() and not k.startswith("sqlite_")) if objmap else 0
        n_views = sum(1 for t in objmap.tables.values() if t.is_view()) if objmap else 0
        cards = [
            ("File size", human_readable_size(size) if size else "—"),
            ("SQLite", db.sqlite_version()),
            ("Encoding", str(db.get_pragma("encoding") or "")),
            ("Tables / views", f"{n_tables} / {n_views}"),
            ("Pages", f"{int(page_count):,} × {int(page_size):,} B"),
            ("Free pages", f"{int(freelist):,}"),
            ("Indices", str(len(objmap.indices)) if objmap else "0"),
            ("Mode", "Read-only" if db.read_only else "Read / write"),
        ]
        for i, (t, v) in enumerate(cards):
            self._stat_card(i // 4, i % 4, t, v)
        self._values = {}
        for name, (w, _getter, setter) in self._editors.items():
            v = db.get_pragma(name)
            self._values[name] = v
            setter(v)
            w.setEnabled(not db.read_only)
        self.btn_save.setEnabled(not db.read_only)

    def save(self) -> None:
        db = self.db
        if db.dirty and QMessageBox.question(self, "Save pragmas",
                                             "Setting PRAGMA values will commit your current transaction.\nAre you sure?") != QMessageBox.Yes:
            return
        changed = 0
        for name, (_w, getter, _setter) in self._editors.items():
            new = getter()
            old = self._values.get(name)
            if str(new).upper() != str(old).upper():
                if not db.set_pragma(name, new):
                    QMessageBox.warning(self, "Save pragmas", f"Setting {name} failed:\n{db.last_error}")
                changed += 1
        self.reload()
        self.pragmasSaved.emit()
        self.statusMessage.emit(f"{changed} pragma{'s' if changed != 1 else ''} updated" if changed else "No pragma changes")

    def title_text(self) -> str:
        return "Database Info"
