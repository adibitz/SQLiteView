"""Spreadsheet-like data grid. Port of upstream ``ExtendedTableWidget`` and ``FilterTableHeader``.

Features: per-column filter row in the header, multi-column sorting (Ctrl+click), copy as
TSV/HTML/SQL, paste from spreadsheets, Delete sets NULL, Ctrl+Shift+click follows foreign
keys, context menu, and a header menu for display formats and conditional formats.
"""
from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import QEvent, QItemSelectionModel, QMimeData, QModelIndex, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QLineEdit, QMenu, QMessageBox, QStyledItemDelegate,
                               QTableView)

from ..core import sqlb
from ..core.data import is_binary, parse_clipboard, value_to_text
from ..core.db import sql_literal
from ..core.query import OrderBy, SortDirection
from ..core.settings import Settings
from ..core.table_model import RawValueRole, SqliteTableModel
from .icons import icon


class FilterHeader(QHeaderView):
    """Horizontal header with a filter line edit under every section."""

    filterChanged = Signal(int, str)
    sortRequested = Signal(int, bool)  # column, additive (Ctrl held)

    def __init__(self, parent: QTableView):
        super().__init__(Qt.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._edits: list[QLineEdit] = []
        self._filters_enabled = True
        self._pending: dict[int, str] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        self.sectionResized.connect(self._adjust_positions)
        self.sectionMoved.connect(self._adjust_positions)
        parent.horizontalScrollBar().valueChanged.connect(self._adjust_positions)
        self.sectionClicked.connect(self._on_section_clicked)

    def set_filters_enabled(self, on: bool) -> None:
        self._filters_enabled = on
        for e in self._edits:
            e.setVisible(on)
        self.updateGeometries()

    def generate_filters(self, count: int) -> None:
        texts = [e.text() for e in self._edits]
        for e in self._edits:
            e.deleteLater()
        self._edits = []
        for i in range(count):
            e = QLineEdit(self)
            e.setProperty("role", "filter")
            e.setPlaceholderText("Filter")
            e.setClearButtonEnabled(True)
            e.setToolTip("Filter: text, =val, <>val, >n, <n, a~b (range), /regex/, =NULL, <>NULL")
            e.textChanged.connect(lambda text, col=i: self._queue(col, text))
            e.setVisible(self._filters_enabled and not self.isSectionHidden(i))
            self._edits.append(e)
        for i, t in enumerate(texts[:count]):
            if t:
                self._edits[i].blockSignals(True)
                self._edits[i].setText(t)
                self._edits[i].blockSignals(False)
        self.updateGeometries()
        self._adjust_positions()

    def clear_filters(self) -> None:
        for e in self._edits:
            e.blockSignals(True)
            e.clear()
            e.setProperty("active", False)
            e.style().unpolish(e)
            e.style().polish(e)
            e.blockSignals(False)

    def set_filter(self, column: int, text: str) -> None:
        if 0 <= column < len(self._edits):
            self._edits[column].setText(text)

    def filter_text(self, column: int) -> str:
        return self._edits[column].text() if 0 <= column < len(self._edits) else ""

    def focus_filter(self, column: int) -> None:
        if 0 <= column < len(self._edits):
            self._edits[column].setFocus()
            self._edits[column].selectAll()

    def _queue(self, column: int, text: str) -> None:
        e = self._edits[column]
        e.setProperty("active", bool(text))
        e.style().unpolish(e)
        e.style().polish(e)
        self._pending[column] = text
        self._timer.start(int(Settings.get("databrowser", "filter_delay")))

    def _flush(self) -> None:
        pending, self._pending = self._pending, {}
        for col, text in pending.items():
            self.filterChanged.emit(col, text)

    def _on_section_clicked(self, section: int) -> None:
        additive = bool(QGuiApplication.keyboardModifiers() & Qt.ControlModifier)
        self.sortRequested.emit(section, additive)

    def _filter_height(self) -> int:
        return (self._edits[0].sizeHint().height() + 4) if (self._edits and self._filters_enabled) else 0

    def sizeHint(self):
        s = super().sizeHint()
        s.setHeight(s.height() + self._filter_height())
        return s

    def updateGeometries(self) -> None:
        h = self._filter_height()
        self.setViewportMargins(0, 0, 0, h)
        super().updateGeometries()
        self._adjust_positions()

    def _adjust_positions(self, *_) -> None:
        h = self._filter_height()
        base = super().sizeHint().height()
        for i, e in enumerate(self._edits):
            if self.isSectionHidden(i) or not self._filters_enabled:
                e.hide()
                continue
            x = self.sectionViewportPosition(i)
            e.setGeometry(QRect(x + 3, base + 1, max(10, self.sectionSize(i) - 6), h - 4))
            e.show()


class CellDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        ed = QLineEdit(parent)
        ed.setFrame(False)
        return ed

    def setEditorData(self, editor, index):
        v = index.data(Qt.EditRole)
        editor.setText("" if v is None else str(v))


class DataGrid(QTableView):
    """Table view used for browsing tables and query results."""

    foreignKeyClicked = Signal(str, str, object)  # table, column, value
    cellActivated = Signal(QModelIndex)
    filterByValueRequested = Signal(int, str)
    editInInspectorRequested = Signal()
    deleteRowsRequested = Signal()
    duplicateRowRequested = Signal()
    insertRowRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_header = FilterHeader(self)
        self.setHorizontalHeader(self.filter_header)
        self.setItemDelegate(CellDelegate(self))
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setWordWrap(False)
        self.setShowGrid(True)
        self.setCornerButtonEnabled(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        vh = self.verticalHeader()
        vh.setDefaultSectionSize(self.fontMetrics().height() + 10)
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setMinimumWidth(36)
        self.horizontalHeader().setMinimumSectionSize(48)
        self.horizontalHeader().setDefaultSectionSize(140)
        self.horizontalHeader().setStretchLastSection(False)
        self.filter_header.sortRequested.connect(self._sort_requested)
        self.filter_header.setContextMenuPolicy(Qt.CustomContextMenu)
        self.filter_header.customContextMenuRequested.connect(self._header_context_menu)
        self._model: Optional[SqliteTableModel] = None

    # ------------------------------------------------------------------ model
    def set_table_model(self, model: SqliteTableModel) -> None:
        self._model = model
        self.setModel(model)
        model.modelReset.connect(self._after_reset)
        model.layoutChanged.connect(self._after_reset)
        self._after_reset()

    def table_model(self) -> Optional[SqliteTableModel]:
        return self._model

    def has_rowid_column(self) -> bool:
        return self._model is not None and not self._model.query.table.is_empty()

    def _after_reset(self) -> None:
        m = self._model
        if m is None:
            return
        n = m.columnCount()
        if self.filter_header.count() != n or len(self.filter_header._edits) != n:
            self.filter_header.generate_filters(n)
        # Column 0 holds the rowid when browsing a table; query results have no such column
        if n:
            self.setColumnHidden(0, self.has_rowid_column())
        self.filter_header._adjust_positions()

    def auto_size_columns(self) -> None:
        if self._model is None:
            return
        fm = self.fontMetrics()
        for c in range(1 if self.has_rowid_column() else 0, self._model.columnCount()):
            title = str(self._model.headerData(c, Qt.Horizontal) or "")
            w = fm.horizontalAdvance(title) + 40
            for r in range(min(self._model.rowCount(), 60)):
                txt = self._model.data(self._model.index(r, c))
                if txt:
                    w = max(w, fm.horizontalAdvance(str(txt)[:60]) + 24)
            self.setColumnWidth(c, max(60, min(w, 360)))

    # ------------------------------------------------------------------ sorting
    def _sort_requested(self, column: int, additive: bool) -> None:
        m = self._model
        # Results of custom SQL can't be re-sorted without changing the user's query
        if m is None or column <= 0 or m.query.table.is_empty():
            return
        name = m.column_name(column)
        order = list(m.query.order_by)
        existing = next((o for o in order if o.expr == name), None)
        if existing is None:
            new = OrderBy(name, SortDirection.Ascending)
            order = (order + [new]) if additive else [new]
        elif existing.direction == SortDirection.Ascending:
            existing = OrderBy(name, SortDirection.Descending)
            order = [existing if o.expr == name else o for o in order] if additive else [existing]
        else:
            order = [o for o in order if o.expr != name] if additive else []
        m.sort_by(order)

    # ------------------------------------------------------------------ clipboard
    def _selected_block(self) -> tuple[list[int], list[int]]:
        idx = [i for i in self.selectedIndexes() if not self.isColumnHidden(i.column())]
        rows = sorted({i.row() for i in idx})
        cols = sorted({i.column() for i in idx}, key=lambda c: self.horizontalHeader().visualIndex(c))
        return rows, cols

    def _ensure_loaded(self, rows: list[int]) -> bool:
        m = self._model
        if m is None:
            return False
        if all(m.row_available(r) for r in rows):
            return True
        if len(rows) > int(Settings.get("databrowser", "complete_threshold")):
            if QMessageBox.question(self, "Copy", "Fetching all selected rows may take a while. Continue?") != QMessageBox.Yes:
                return False
        m.complete_cache()
        return True

    def copy(self, with_headers: bool = False, as_sql: bool = False) -> None:
        m = self._model
        rows, cols = self._selected_block()
        if m is None or not rows or not cols:
            return
        if not self._ensure_loaded(rows):
            return
        selected = {(i.row(), i.column()) for i in self.selectedIndexes()}
        mime = QMimeData()
        if len(rows) == 1 and len(cols) == 1 and not with_headers and not as_sql:
            v = m.raw_value(rows[0], cols[0])
            if isinstance(v, (bytes, bytearray)) and is_binary(v):
                mime.setData("application/octet-stream", bytes(v))
                from PySide6.QtGui import QImage
                img = QImage()
                if img.loadFromData(bytes(v)):
                    mime.setImageData(img)
            mime.setText(value_to_text(v))
            QGuiApplication.clipboard().setMimeData(mime)
            return
        if as_sql:
            table = m.query.table.to_string() if not m.query.table.is_empty() else '"table"'
            names = ",".join(sqlb.escape_identifier(m.column_name(c)) for c in cols)
            lines = []
            for r in rows:
                vals = ",".join(sql_literal(m.raw_value(r, c)) if (r, c) in selected else "NULL" for c in cols)
                lines.append(f"INSERT INTO {table} ({names}) VALUES ({vals});")
            mime.setText("\n".join(lines))
            QGuiApplication.clipboard().setMimeData(mime)
            return
        tsv, htm = [], ["<table>"]
        if with_headers:
            tsv.append("\t".join(m.column_name(c) for c in cols))
            htm.append("<tr>" + "".join(f"<th>{html.escape(m.column_name(c))}</th>" for c in cols) + "</tr>")
        for r in rows:
            vals = []
            hv = []
            for c in cols:
                v = m.raw_value(r, c) if (r, c) in selected else None
                t = "" if v is None else value_to_text(v)
                if any(ch in t for ch in "\t\n\"") and t:
                    t_tsv = '"' + t.replace('"', '""') + '"'
                else:
                    t_tsv = t
                vals.append(t_tsv)
                hv.append(f"<td>{html.escape(t)}</td>")
            tsv.append("\t".join(vals))
            htm.append("<tr>" + "".join(hv) + "</tr>")
        htm.append("</table>")
        mime.setText("\r\n".join(tsv) + "\r\n")
        mime.setHtml("".join(htm))
        QGuiApplication.clipboard().setMimeData(mime)

    def paste(self) -> None:
        m = self._model
        if m is None or not m.is_editable():
            return
        data = parse_clipboard(QGuiApplication.clipboard().text())
        if not data:
            return
        rows, cols = self._selected_block()
        cur = self.currentIndex()
        start_row = rows[0] if rows else max(0, cur.row())
        start_col = cols[0] if cols else max(1, cur.column())
        vis_cols = [c for c in sorted(range(1, m.columnCount()), key=lambda c: self.horizontalHeader().visualIndex(c))
                    if not self.isColumnHidden(c)]
        if start_col not in vis_cols:
            return
        ci = vis_cols.index(start_col)
        # Single value pasted into a larger selection fills the whole selection (upstream behaviour)
        if len(data) == 1 and len(data[0]) == 1 and len(rows) * len(cols) > 1:
            for r in rows:
                for c in cols:
                    m.setData(m.index(r, c), data[0][0])
            return
        needed = start_row + len(data) - m.rowCount()
        if needed > 0:
            if QMessageBox.question(self, "Paste", f"The pasted data needs {needed} more row(s). Add them?") != QMessageBox.Yes:
                data = data[: len(data) - needed]
            else:
                m.insertRows(m.rowCount(), needed)
        m.db.set_undo_savepoint()
        for dr, row in enumerate(data):
            for dc, val in enumerate(row):
                if ci + dc >= len(vis_cols):
                    break
                m.setData(m.index(start_row + dr, vis_cols[ci + dc]), val)

    def set_selection_null(self) -> None:
        m = self._model
        if m is None or not m.is_editable():
            return
        idx = [i for i in self.selectedIndexes() if not self.isColumnHidden(i.column())]
        if not idx:
            return
        m.db.set_undo_savepoint()
        for i in idx:
            if m.is_editable(i):
                m.set_typed_data(i, False, None)

    # ------------------------------------------------------------------ events
    def keyPressEvent(self, e) -> None:
        if e.matches(QKeySequence.StandardKey.Copy):
            self.copy(with_headers=bool(e.modifiers() & Qt.ShiftModifier))
            return
        if e.matches(QKeySequence.StandardKey.Paste):
            self.paste()
            return
        if e.key() == Qt.Key_C and e.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier):
            self.copy(with_headers=True)
            return
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.state() != QAbstractItemView.EditingState:
            self.set_selection_null()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.state() != QAbstractItemView.EditingState \
                and e.modifiers() & Qt.ControlModifier:
            self.editInInspectorRequested.emit()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and (e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)) == \
                (Qt.ControlModifier | Qt.ShiftModifier):
            idx = self.indexAt(e.position().toPoint())
            m = self._model
            if idx.isValid() and m is not None:
                fk = m.foreign_key_for(idx.column())
                if fk is not None:
                    col = fk.columns[0] if fk.columns else ""
                    self.foreignKeyClicked.emit(fk.table, col, m.raw_value(idx.row(), idx.column()))
                    return
        super().mousePressEvent(e)

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:
        super().currentChanged(current, previous)
        self.cellActivated.emit(current)

    # ------------------------------------------------------------------ context menu
    def _context_menu(self, pos: QPoint) -> None:
        m = self._model
        if m is None:
            return
        idx = self.indexAt(pos)
        editable = m.is_editable()
        menu = QMenu(self)
        a = menu.addAction(icon("copy"), "Copy")
        a.setShortcut(QKeySequence.Copy)
        a.triggered.connect(lambda: self.copy())
        menu.addAction(icon("copy"), "Copy with Headers", lambda: self.copy(with_headers=True))
        menu.addAction(icon("file-code"), "Copy as SQL INSERT", lambda: self.copy(as_sql=True))
        a = menu.addAction(icon("clipboard"), "Paste", self.paste)
        a.setEnabled(editable)
        menu.addSeparator()
        if idx.isValid():
            v = m.raw_value(idx.row(), idx.column())
            txt = "NULL" if v is None else value_to_text(v)
            if not is_binary(v):
                short = txt if len(txt) < 30 else txt[:27] + "…"
                fa = menu.addAction(icon("filter"), f"Filter by “{short}”")
                fa.triggered.connect(lambda: self.filterByValueRequested.emit(idx.column(), "=NULL" if v is None else "=" + txt))
                if m.foreign_key_for(idx.column()) is not None and v is not None:
                    fk = m.foreign_key_for(idx.column())
                    menu.addAction(icon("link"), f"Go to referenced row in {fk.table}",
                                   lambda: self.foreignKeyClicked.emit(fk.table, fk.columns[0] if fk.columns else "", v))
        menu.addAction(icon("edit"), "Edit in Inspector", self.editInInspectorRequested.emit)
        a = menu.addAction(icon("null"), "Set to NULL", self.set_selection_null)
        a.setEnabled(editable)
        menu.addSeparator()
        a = menu.addAction(icon("row-plus"), "Insert New Record", self.insertRowRequested.emit)
        a.setEnabled(editable)
        a = menu.addAction(icon("duplicate"), "Duplicate Record", self.duplicateRowRequested.emit)
        a.setEnabled(editable and idx.isValid())
        a = menu.addAction(icon("trash"), "Delete Record(s)", self.deleteRowsRequested.emit)
        a.setEnabled(editable and idx.isValid())
        menu.exec(self.viewport().mapToGlobal(pos))

    def _header_context_menu(self, pos: QPoint) -> None:
        m = self._model
        col = self.filter_header.logicalIndexAt(pos)
        if m is None or col < 0 or (col == 0 and self.has_rowid_column()):
            return
        name = m.column_name(col)
        menu = QMenu(self)
        if self.has_rowid_column():
            menu.addAction(icon("format"), "Display Format…", lambda: self.edit_display_format(col))
            a = menu.addAction(icon("x"), "Reset Display Format", lambda: m.set_display_format(col, ""))
            a.setEnabled(m.has_display_format(col))
            menu.addSeparator()
        menu.addAction(icon("sparkle"), "Conditional Formats…", lambda: self.edit_cond_formats(col))
        a = menu.addAction(icon("x"), "Clear Conditional Formats", lambda: m.set_cond_formats(name, []))
        a.setEnabled(bool(m.cond_formats(name)))
        menu.exec(self.filter_header.mapToGlobal(pos))

    def edit_display_format(self, column: int) -> None:
        from .dialogs.display_format import DisplayFormatDialog
        m = self._model
        if m is None or not self.has_rowid_column():
            return
        dlg = DisplayFormatDialog(m.db, m.query.table, m.column_name(column), m.display_format(column), self)
        if dlg.exec():
            m.set_display_format(column, dlg.selected_format())

    def edit_cond_formats(self, column: int) -> None:
        from .dialogs.cond_format import CondFormatDialog
        m = self._model
        if m is None:
            return
        name = m.column_name(column)
        dlg = CondFormatDialog(name, m.cond_formats(name), m.filter_escape, self)
        if dlg.exec():
            m.set_cond_formats(name, dlg.formats())

    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.selectedIndexes()})

    def select_cell(self, row: int, column: int) -> None:
        if self._model is None:
            return
        ix = self._model.index(row, column)
        self.setCurrentIndex(ix)
        self.selectionModel().select(ix, QItemSelectionModel.ClearAndSelect)
        self.scrollTo(ix, QAbstractItemView.PositionAtCenter)
