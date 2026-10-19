"""Start page shown when no database is open."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QWidget)

from .. import APP_DISPLAY_NAME, __version__
from ..core.data import human_readable_size
from .icons import bind_icon, icon
from .widgets import label


class WelcomePage(QWidget):
    newRequested = Signal()
    openRequested = Signal()
    openReadOnlyRequested = Signal()
    memoryRequested = Signal()
    recentRequested = Signal(str)
    paletteRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "surface")
        self.setAcceptDrops(False)
        outer = QHBoxLayout(self)
        outer.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(18)
        col.addStretch(1)

        hero = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icon("database", "accent").pixmap(52, 52))
        hero.addWidget(logo)
        tl = QVBoxLayout()
        tl.setSpacing(0)
        tl.addWidget(label(APP_DISPLAY_NAME, "title"))
        tl.addWidget(label(f"v{__version__} · Open, browse, edit and query SQLite databases", "muted"))
        hero.addLayout(tl)
        hero.addStretch(1)
        col.addLayout(hero)

        grid = QGridLayout()
        grid.setSpacing(10)
        tiles = [
            ("database-plus", "New Database", "Create an empty SQLite file", self.newRequested, "Ctrl+N"),
            ("folder-open", "Open Database", "Browse for a .db / .sqlite file", self.openRequested, "Ctrl+O"),
            ("lock", "Open Read-Only", "Inspect without risk of changes", self.openReadOnlyRequested, "Ctrl+Shift+O"),
            ("memory", "In-Memory Database", "Scratch database, nothing saved", self.memoryRequested, ""),
        ]
        for i, (ic, title, sub, sig, sc) in enumerate(tiles):
            b = QPushButton(f"{title}\n{sub}")
            b.setProperty("kind", "tile")
            bind_icon(b, ic, "accent")
            b.setIconSize(QSize(22, 22))
            b.setMinimumSize(250, 64)
            b.setCursor(Qt.PointingHandCursor)
            if sc:
                b.setToolTip(sc)
            b.clicked.connect(sig.emit)
            grid.addWidget(b, i // 2, i % 2)
        col.addLayout(grid)

        col.addWidget(label("RECENT", "section"))
        self.recent = QListWidget()
        self.recent.setFrameShape(QFrame.NoFrame)
        self.recent.setIconSize(QSize(18, 18))
        self.recent.setMinimumHeight(220)
        self.recent.setMaximumWidth(520)
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recent.setTextElideMode(Qt.ElideMiddle)
        self.recent.itemActivated.connect(lambda it: self.recentRequested.emit(it.data(Qt.UserRole)))
        self.recent.itemClicked.connect(lambda it: self.recentRequested.emit(it.data(Qt.UserRole)))
        col.addWidget(self.recent)
        self.no_recent = label("No recent databases. Drop a file anywhere in this window to open it.", "faint")
        col.addWidget(self.no_recent)
        tip = QPushButton("Tip: press Ctrl+K to search commands, tables and columns")
        tip.setProperty("kind", "link")
        tip.setCursor(Qt.PointingHandCursor)
        tip.clicked.connect(self.paletteRequested.emit)
        col.addWidget(tip)
        col.addStretch(2)
        outer.addLayout(col)
        outer.addStretch(1)

    def set_recent(self, files: list[str]) -> None:
        self.recent.clear()
        shown = 0
        for path in files:
            exists = os.path.exists(path)
            name = os.path.basename(path) or path
            folder = os.path.dirname(path)
            meta = folder if len(folder) <= 48 else folder[:20] + "…" + folder[-26:]
            if exists:
                st = os.stat(path)
                meta += f"   ·   {human_readable_size(st.st_size)}   ·   {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"
            else:
                meta += "   ·   missing"
            it = QListWidgetItem(icon("database" if exists else "alert", "accent" if exists else "text_faint"), f"{name}\n{meta}")
            it.setData(Qt.UserRole, path)
            it.setToolTip(path)
            if not exists:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            self.recent.addItem(it)
            shown += 1
        self.recent.setVisible(shown > 0)
        self.no_recent.setVisible(shown == 0)
