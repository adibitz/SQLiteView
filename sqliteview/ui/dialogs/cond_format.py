"""Conditional formats editor. Port of upstream ``CondFormatManager``.

Each row is a rule: a filter in the grid's filter syntax plus a text and/or background colour.
Rules are checked top to bottom and the first match wins.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QMenu, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout)

from ...core.condformat import CondFormat
from ...core.filters import filter_to_sql_condition
from ..theme import tokens
from ..widgets import label, tool_button

COL_FILTER, COL_FG, COL_BG = 0, 1, 2


def _swatch(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(QColor(color) if color else Qt.transparent)
    return QIcon(pm)


class ColorButton(QToolButton):
    """Shows a colour swatch; click to pick a colour, or reset it to the default."""

    def __init__(self, color: str = "", on_change=None, parent=None):
        super().__init__(parent)
        self._color = ""
        self._on_change = on_change
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        menu.addAction("Choose colour…", self._choose)
        menu.addAction("Default", lambda: self.set_color(""))
        self.setMenu(menu)
        self.set_color(color, notify=False)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str, notify: bool = True) -> None:
        self._color = color
        self.setIcon(_swatch(color))
        self.setText(color.upper() if color else "Default")
        if notify and self._on_change is not None:
            self._on_change()

    def _choose(self) -> None:
        c = QColorDialog.getColor(QColor(self._color or tokens().text), self, "Choose colour")
        if c.isValid():
            self.set_color(c.name())


class CondFormatDialog(QDialog):
    def __init__(self, column: str, formats: list[CondFormat], escape_character: str = "\\", parent=None):
        super().__init__(parent)
        self.escape_character = escape_character
        self.setWindowTitle("Conditional Formats")
        self.resize(620, 400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Conditional Formats", "title"))
        lay.addWidget(label(f"Colour cells of column “{column}”. Conditions use the filter syntax: text, =val, <>val, "
                            ">n, <n, a~b, /regex/. An empty condition matches every value. The first matching rule wins.",
                            "muted", wrap=True))
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.btn_add = tool_button("plus", "Add rule", text="Add", on_click=self.add_rule)
        self.btn_remove = tool_button("trash", "Remove selected rule", on_click=self.remove_rule)
        self.btn_up = tool_button("arrow-up", "Move up", on_click=lambda: self._move(-1))
        self.btn_down = tool_button("arrow-down", "Move down", on_click=lambda: self._move(1))
        for b in (self.btn_add, self.btn_remove, self.btn_up, self.btn_down):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Condition", "Text colour", "Background"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_FILTER, QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_FG, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_BG, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._update)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)
        self.error = label("", "error", wrap=True)
        lay.addWidget(self.error)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)
        for f in formats:
            self._append_row(f.filter, f.fg, f.bg)
        self._update()

    # ------------------------------------------------------------------ rows
    def _append_row(self, filter_text: str, fg: str, bg: str) -> int:
        r = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(r)
        self.table.setItem(r, COL_FILTER, QTableWidgetItem(filter_text))
        self.table.setCellWidget(r, COL_FG, ColorButton(fg, self._update))
        self.table.setCellWidget(r, COL_BG, ColorButton(bg, self._update))
        self.table.blockSignals(False)
        return r

    def add_rule(self) -> None:
        r = self._append_row("", "", tokens().accent_soft)
        self.table.selectRow(r)
        self.table.editItem(self.table.item(r, COL_FILTER))
        self._update()

    def remove_rule(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self._update()

    def _move(self, delta: int) -> None:
        r = self.table.currentRow()
        to = r + delta
        if r < 0 or not 0 <= to < self.table.rowCount():
            return
        rows = self._rows()
        rows[r], rows[to] = rows[to], rows[r]
        self.table.setRowCount(0)
        for f, fg, bg in rows:
            self._append_row(f, fg, bg)
        self.table.selectRow(to)
        self._update()

    def _rows(self) -> list[tuple[str, str, str]]:
        return [(self.table.item(r, COL_FILTER).text().strip(), self.table.cellWidget(r, COL_FG).color(),
                 self.table.cellWidget(r, COL_BG).color()) for r in range(self.table.rowCount())]

    # ------------------------------------------------------------------ state
    def _update(self, *_) -> None:
        self.table.blockSignals(True)
        bad = []
        for r, (f, fg, bg) in enumerate(self._rows()):
            it = self.table.item(r, COL_FILTER)
            # The condition cell previews the formatting
            it.setForeground(QColor(fg) if fg else QColor(tokens().text))
            it.setBackground(QColor(bg) if bg else QColor(tokens().surface))
            if f and not filter_to_sql_condition(f, self.escape_character):
                bad.append(f)
        self.table.blockSignals(False)
        self.error.setText(f"Condition “{bad[0]}” never matches anything." if bad else "")
        self._update_buttons()

    def _update_buttons(self) -> None:
        r = self.table.currentRow()
        self.btn_remove.setEnabled(r >= 0)
        self.btn_up.setEnabled(r > 0)
        self.btn_down.setEnabled(0 <= r < self.table.rowCount() - 1)

    def formats(self) -> list[CondFormat]:
        return [CondFormat(f, fg, bg, self.escape_character) for f, fg, bg in self._rows() if fg or bg]
