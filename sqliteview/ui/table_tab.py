"""Document tab for browsing one table or view (replaces upstream's "Browse Data" tab)."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMenu, QMessageBox, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QToolButton, QVBoxLayout, QWidget)

from .. import APP_DISPLAY_NAME
from ..core import sqlb
from ..core.db import DBBrowserDB
from ..core.filters import describe_filter, tokenize_global_filter
from ..core.rowexport import ROW_FORMATS, format_for_path, format_rows
from ..core.settings import Settings
from ..core.project import TableSettings
from ..core.query import OrderBy, Query, SortDirection
from ..core.table_model import RowCount, SqliteTableModel
from .data_grid import DataGrid
from .icons import icon
from .sql_editor import EditorWithFind, SqlEditor
from .widgets import IntField, SearchLineEdit, SegmentedControl, label, tool_button, vsep


class StructureView(QWidget):
    """Read-only overview of a table's columns, keys, indices and triggers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        lay.addWidget(label("COLUMNS", "section"))
        self.columns = self._make_table(["", "Name", "Type", "Not null", "Unique", "Default", "Check", "Collation", "References"])
        lay.addWidget(self.columns, 3)
        lay.addWidget(label("INDICES", "section"))
        self.indices = self._make_table(["Name", "Unique", "Columns", "Where"])
        lay.addWidget(self.indices, 1)
        lay.addWidget(label("TRIGGERS", "section"))
        self.triggers = self._make_table(["Name", "Definition"])
        lay.addWidget(self.triggers, 1)

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        t.setShowGrid(False)
        t.setFrameShape(QFrame.StyledPanel)
        return t

    def populate(self, db: DBBrowserDB, ident: sqlb.ObjectIdentifier) -> None:
        t = db.get_table_by_name(ident)
        if t is None:
            return
        pk = t.primary_key_names()
        rows = []
        for f in t.fields:
            fk = t.foreign_key([f.name])
            rows.append([
                "PK" if f.name in pk else "", f.name, f.type, "✓" if f.notnull else "", "✓" if f.unique else "",
                f.default_value, f.check, f.collation, fk.to_string() if fk else
                (f"GENERATED {f.generated.storage}: {f.generated.expression}" if f.generated else ""),
            ])
        self._fill(self.columns, rows)
        for r, f in enumerate(t.fields):
            item = self.columns.item(r, 0)
            if f.name in pk:
                item.setIcon(icon("key", "warning"))
                item.setText("")
                item.setToolTip("Primary key" + (" (AUTOINCREMENT)" if t.primary_key() and t.primary_key().auto_increment else ""))
            elif t.foreign_key([f.name]):
                item.setIcon(icon("link", "accent"))
        self.columns.resizeColumnsToContents()
        objmap = db.schemata.get(ident.schema)
        idx_rows, trg_rows = [], []
        if objmap:
            for name, idx in objmap.indices.items():
                if idx.base_table() == t.name:
                    cols = ", ".join(c.name + (f" {c.order}" if c.order else "") for c in idx.fields) or "(auto)"
                    idx_rows.append([name, "✓" if idx.unique else "", cols, idx.where_expr])
            for name, trg in objmap.triggers.items():
                if trg.base_table() == t.name:
                    trg_rows.append([name, " ".join(trg.original_sql.split())])
        self._fill(self.indices, idx_rows)
        self._fill(self.triggers, trg_rows)
        self.indices.resizeColumnsToContents()

    @staticmethod
    def _fill(table: QTableWidget, rows: list[list[str]]) -> None:
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(v))


class TableTab(QWidget):
    """Browse/edit a table: data grid with filters, structure overview, DDL."""

    cellSelected = Signal(object, QModelIndex)  # model, index
    foreignKeyJump = Signal(str, str, object)
    modifyRequested = Signal(object)  # ObjectIdentifier
    exportRequested = Signal(object)
    exportViewRequested = Signal(object)  # SqliteTableModel: all rows matching the filters, in sort order
    insertDialogRequested = Signal(object)
    statusMessage = Signal(str)
    fullScreenRequested = Signal(bool)

    kind = "table"

    def __init__(self, db: DBBrowserDB, ident: sqlb.ObjectIdentifier, parent=None):
        super().__init__(parent)
        self.db = db
        self.ident = ident
        self.model = SqliteTableModel(db, self)
        self.model.set_page_size(int(Settings.get("databrowser", "page_size") or 0))
        self._build()
        self.model.pagingChanged.connect(self._update_footer)
        self.model.finishedRowCount.connect(self._update_footer)
        self.model.finishedFetch.connect(lambda *_: self._update_footer())
        self.model.errorOccurred.connect(self._on_error)
        self.model.dataChanged.connect(lambda *_: self.statusMessage.emit(""))
        self._first_load = True
        self.model.finishedFetch.connect(self._maybe_autosize)
        self.reload()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setProperty("role", "toolbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(4)
        self.title_icon = QLabel()
        self.title = label("", "h2")
        self.kind_chip = label("", "chip")
        bl.addWidget(self.title_icon)
        bl.addWidget(self.title)
        bl.addWidget(self.kind_chip)
        bl.addSpacing(10)
        self.view_switch = SegmentedControl([("Data", "table"), ("Structure", "column"), ("DDL", "file-code")])
        self.view_switch.changed.connect(self._switch_view)
        bl.addWidget(self.view_switch)
        bl.addStretch(1)
        self.search = SearchLineEdit("Search in all columns…")
        self.search.setFixedWidth(240)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._apply_global_filter)
        self.search.textChanged.connect(lambda: self._search_timer.start(250))
        bl.addWidget(self.search)
        bl.addWidget(vsep())
        self.btn_refresh = tool_button("refresh", "Refresh", on_click=self.reload, shortcut="F5")
        self.btn_clear = tool_button("filter-x", "Clear all filters and sorting", on_click=self.clear_filters)
        self.btn_add = tool_button("row-plus", "New record", on_click=self.add_record, shortcut="Ctrl+N")
        self.btn_insert = tool_button("plus", "Insert record with values…", on_click=lambda: self.insertDialogRequested.emit(self.ident))
        self.btn_dup = tool_button("duplicate", "Duplicate selected record", on_click=self.duplicate_record)
        self.btn_del = tool_button("trash", "Delete selected record(s)", on_click=self.delete_records, shortcut="Del")
        self.btn_export = tool_button("download", "Export…")
        self.btn_export.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.btn_export)
        export_menu.addAction(icon("table"), "Export Whole Table…", lambda: self.exportRequested.emit(self.ident))
        export_menu.addAction(icon("filter"), "Export Filtered && Sorted Rows…", lambda: self.exportViewRequested.emit(self.model))
        export_menu.addAction(icon("list"), "Export Current Page…", self.export_page)
        self.act_export_selected = export_menu.addAction(icon("check"), "Export Selected Rows…", self.export_selected)
        export_menu.aboutToShow.connect(lambda: self.act_export_selected.setEnabled(bool(self.grid.selectedIndexes())))
        self.btn_export.setMenu(export_menu)
        self.btn_rows = tool_button("list", "Select whole rows instead of single cells", checkable=True,
                                    on_click=lambda: self.grid.set_row_selection(self.btn_rows.isChecked()))
        self.btn_modify = tool_button("table-edit", "Modify table structure…", on_click=lambda: self.modifyRequested.emit(self.ident))
        # Views: choosing a pseudo primary key makes them editable (upstream "Unlock view editing")
        self.btn_pk = tool_button("key", "Select a column to use as primary key, to edit this view")
        self.btn_pk.setPopupMode(QToolButton.InstantPopup)
        self.pk_menu = QMenu(self.btn_pk)
        self.pk_menu.aboutToShow.connect(self._fill_pk_menu)
        self.btn_pk.setMenu(self.pk_menu)
        self.btn_expand = tool_button("maximize", "Show table full screen (F11, Esc to exit)", checkable=True,
                                      on_click=lambda: self.set_expanded(self.btn_expand.isChecked()))
        for b in (self.btn_refresh, self.btn_clear, self.btn_rows, self.btn_pk, self.btn_add, self.btn_insert, self.btn_dup,
                  self.btn_del, self.btn_export, self.btn_modify, self.btn_expand):
            bl.addWidget(b)
        lay.addWidget(bar)

        # Active filters / search / sort as removable chips
        self.chips_bar = QFrame()
        self.chips_bar.setProperty("role", "chipsbar")
        self.chips_layout = QHBoxLayout(self.chips_bar)
        self.chips_layout.setContentsMargins(12, 5, 10, 5)
        self.chips_layout.setSpacing(6)
        self.chips_bar.hide()
        lay.addWidget(self.chips_bar)

        self.stack = QStackedWidget()
        self.grid = DataGrid()
        self.grid.set_table_model(self.model)
        self.grid.filter_header.filterChanged.connect(self._on_filter)
        self.grid.cellActivated.connect(lambda ix: self.cellSelected.emit(self.model, ix))
        self.grid.foreignKeyClicked.connect(self.foreignKeyJump)
        self.grid.filterByValueRequested.connect(self.grid.filter_header.set_filter)
        self.grid.deleteRowsRequested.connect(self.delete_records)
        self.grid.duplicateRowRequested.connect(self.duplicate_record)
        self.grid.insertRowRequested.connect(self.add_record)
        self.grid.rowDetailsRequested.connect(self.show_row_details)
        self.grid.exportSelectionRequested.connect(self.export_selected)
        self.grid.selectionSummaryChanged.connect(self._update_selection_bar)
        self.stack.addWidget(self.grid)
        self.structure = StructureView()
        self.stack.addWidget(self.structure)
        self.ddl = SqlEditor(read_only=True)
        self.stack.addWidget(EditorWithFind(self.ddl))
        lay.addWidget(self.stack, 1)

        foot = QFrame()
        foot.setProperty("role", "surface")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(12, 4, 10, 4)
        fl.setSpacing(10)
        self.rows_label = label("", "muted")
        self.loading = label("Loading…", "faint")
        self.loading.hide()
        fl.addWidget(self.rows_label)
        fl.addWidget(self.loading)

        # contextual bar for the current selection
        self.sel_bar = QFrame()
        self.sel_bar.setProperty("role", "selectionbar")
        sl = QHBoxLayout(self.sel_bar)
        sl.setContentsMargins(10, 1, 4, 1)
        sl.setSpacing(2)
        self.sel_label = label("", "")
        sl.addWidget(self.sel_label)
        self.btn_sel_copy = tool_button("copy", "Copy selected rows as…")
        self.btn_sel_copy.setPopupMode(QToolButton.InstantPopup)
        copy_menu = QMenu(self.btn_sel_copy)
        for key, text in ROW_FORMATS.items():
            copy_menu.addAction(text, lambda k=key: self._copy_rows(k))
        self.btn_sel_copy.setMenu(copy_menu)
        self.btn_sel_export = tool_button("download", "Export selected rows…", on_click=self.export_selected)
        self.btn_sel_null = tool_button("null", "Set selected cells to NULL", on_click=lambda: self.grid.set_selection_null())
        self.btn_sel_delete = tool_button("trash", "Delete selected rows", on_click=self.delete_records)
        self.btn_sel_clear = tool_button("x", "Clear selection", on_click=lambda: self.grid.clearSelection())
        for b in (self.btn_sel_copy, self.btn_sel_export, self.btn_sel_null, self.btn_sel_delete, self.btn_sel_clear):
            sl.addWidget(b)
        self.sel_bar.hide()
        fl.addWidget(self.sel_bar)
        fl.addStretch(1)

        # pagination
        fl.addWidget(label("Rows per page", "faint"))
        self.page_size = QComboBox()
        for n in (50, 100, 250, 500, 1000, 5000):
            self.page_size.addItem(f"{n:,}", n)
        self.page_size.addItem("All", 0)
        self.page_size.setCurrentIndex(max(0, self.page_size.findData(self.model.page_size)))
        self.page_size.setToolTip("“All” shows every row in one list that loads as you scroll")
        self.page_size.currentIndexChanged.connect(self._page_size_changed)
        fl.addWidget(self.page_size)
        self.pager = QWidget()
        pl = QHBoxLayout(self.pager)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(2)
        self.btn_first = tool_button("chevrons-left", "First page", on_click=lambda: self.model.set_page(0))
        self.btn_prev = tool_button("chevron-left", "Previous page (Alt+Left)", on_click=lambda: self.model.set_page(self.model.page - 1))
        self.page_spin = IntField(1, 1)
        self.page_spin.setFixedWidth(64)
        self.page_spin.setToolTip("Type a page number and press Enter")
        self.page_spin.editingFinished.connect(lambda: self.model.set_page(self.page_spin.value() - 1))
        self.page_of = label("of 1", "faint")
        self.btn_next = tool_button("chevron-right", "Next page (Alt+Right)", on_click=lambda: self.model.set_page(self.model.page + 1))
        self.btn_last = tool_button("chevrons-right", "Last page", on_click=lambda: self.model.set_page(self.model.page_count() - 1))
        pl.addWidget(self.btn_first)
        pl.addWidget(self.btn_prev)
        pl.addWidget(label("Page", "faint"))
        pl.addWidget(self.page_spin)
        pl.addWidget(self.page_of)
        pl.addWidget(self.btn_next)
        pl.addWidget(self.btn_last)
        fl.addWidget(self.pager)
        from PySide6.QtGui import QKeySequence, QShortcut
        for seq, delta in (("Alt+Left", -1), ("Alt+Right", 1)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda d=delta: self.model.set_page(self.model.page + d))
        fl.addWidget(label("Go to row", "faint"))
        self.goto = IntField(1, 1)
        self.goto.setFixedWidth(90)
        self.goto.setToolTip("Type a row number and press Enter")
        self.goto.editingFinished.connect(self._goto_row)
        fl.addWidget(self.goto)
        lay.addWidget(foot)

    # ------------------------------------------------------------------ behaviour
    @property
    def table(self) -> Optional[sqlb.Table]:
        return self.db.get_table_by_name(self.ident)

    def title_text(self) -> str:
        return self.ident.to_display_string()

    def reload(self) -> None:
        t = self.table
        if t is None:
            return
        is_view = t.is_view()
        self.title.setText(self.ident.to_display_string())
        self.title_icon.setPixmap(icon("view" if is_view else "table", "accent").pixmap(18, 18))
        chip = "VIEW" if is_view else ("VIRTUAL" if t.is_virtual() else ("WITHOUT ROWID" if t.without_rowid else "TABLE"))
        if self.ident.schema != "main":
            chip += f" · {self.ident.schema}"
        self.kind_chip.setText(chip)
        q = Query(self.ident, is_view=is_view)
        old = self.model.query
        if old.table == self.ident:
            q.where = dict(old.where)
            q.global_where = list(old.global_where)
            q.order_by = list(old.order_by)
            q.selected_columns = list(old.selected_columns)  # display formats
            q.rowid_columns = list(old.rowid_columns) if is_view else q.rowid_columns
            if is_view and q.has_custom_rowid_column() and not set(q.rowid_columns) <= set(t.field_names()):
                q.rowid_columns = ["_rowid_"]  # the pseudo primary key column is gone from the view
        self.loading.show()
        page = self.model.page if old.table == self.ident else 0
        self.model.set_table_query(q)
        if page:
            self.model.set_page(page)
        editable = self.model.is_editable()
        for b in (self.btn_add, self.btn_insert, self.btn_dup, self.btn_del):
            b.setEnabled(editable)
        self.btn_modify.setEnabled(not is_view and not t.is_virtual() and not self.db.read_only
                                   and not t.name.startswith("sqlite_"))
        self._update_pk_button(is_view)
        self.structure.populate(self.db, self.ident)
        sql = t.original_sql or t.sql(self.ident.schema)
        self.ddl.setPlainText(sql.rstrip(";") + ";")
        self._update_footer()

    def _maybe_autosize(self, begin: int, end: int) -> None:
        if self._first_load and end > begin:
            self._first_load = False
            self.grid.auto_size_columns()

    def _switch_view(self, i: int) -> None:
        self.stack.setCurrentIndex(i)
        self.search.setVisible(i == 0)

    def show_view(self, i: int) -> None:
        self.view_switch.set_current(i)
        self._switch_view(i)

    def _on_filter(self, column: int, text: str) -> None:
        self.loading.show()
        self.model.update_filter(column, text)
        self._update_footer()

    def _apply_global_filter(self) -> None:
        self.loading.show()
        self.model.update_global_filter(tokenize_global_filter(self.search.text()))
        self._update_footer()

    def clear_filters(self) -> None:
        self.grid.filter_header.clear_filters()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.model.clear_filters()
        self.model.sort_by([])
        self._update_footer()

    def set_filter(self, column_name: str, value: str) -> None:
        c = self.model.column_index(column_name)
        if c > 0:
            self.grid.filter_header.set_filter(c, value)

    def _update_footer(self) -> None:
        m = self.model
        complete = m.row_count_available() == RowCount.Complete
        total = m.total_rows()
        counting = "" if complete else "+"
        filtered = " (filtered)" if (m.filter_count() or m.query.global_where) else ""
        if m.page_size and total:
            first = m.row_offset() + 1
            last = m.row_offset() + m.rowCount()
            self.rows_label.setText(f"Showing {first:,}–{last:,} of {total:,}{counting} rows{filtered}")
        else:
            self.rows_label.setText(f"{total:,}{counting} row{'s' if total != 1 else ''}{filtered}"
                                    + ("" if complete else " — counting…"))
        self.loading.setVisible(m.reading_data())
        paged = bool(m.page_size)
        self.pager.setVisible(paged)
        pages = m.page_count()
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, max(1, pages))
        self.page_spin.setValue(m.page + 1)
        self.page_spin.blockSignals(False)
        self.page_of.setText(f"of {pages:,}{counting}")
        self.btn_first.setEnabled(paged and m.page > 0)
        self.btn_prev.setEnabled(paged and m.page > 0)
        self.btn_next.setEnabled(paged and (m.page < pages - 1 or not complete))
        self.btn_last.setEnabled(paged and m.page < pages - 1)
        self.goto.setRange(1, max(1, total))
        self._update_chips()

    def _page_size_changed(self) -> None:
        size = int(self.page_size.currentData())
        Settings.set("databrowser", "page_size", size)
        self.model.set_page_size(size)

    def _goto_row(self) -> None:
        r = self.goto.value() - 1  # absolute row number
        m = self.model
        if r < 0 or r >= max(m.total_rows(), m.rowCount()):
            return
        if m.page_size:
            m.set_page(r // m.page_size)
            r -= m.row_offset()
        if 0 <= r < m.rowCount():
            col = max(1, self.grid.currentIndex().column())
            self.grid.select_cell(r, col)

    # ------------------------------------------------------------------ active filter chips
    def _chip(self, text: str, tip: str, on_remove) -> QToolButton:
        b = QToolButton()
        b.setText(text + "   ✕")
        b.setToolTip(tip)
        b.setProperty("kind", "chip")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(on_remove)
        return b

    def _update_chips(self) -> None:
        while self.chips_layout.count():
            it = self.chips_layout.takeAt(0)
            if it.widget():
                it.widget().hide()  # deleteLater only runs on the next event-loop pass
                it.widget().deleteLater()
        m = self.model
        chips = []
        for c in range(1, m.columnCount()):
            text = self.grid.filter_header.filter_text(c)
            if text and m.column_name(c) in m.query.where:
                chips.append(self._chip(f"{m.column_name(c)} {describe_filter(text)}", "Remove this filter",
                                        lambda _=False, col=c: self.grid.filter_header.set_filter(col, "")))
        if self.search.text().strip() and m.query.global_where:
            chips.append(self._chip(f"Search “{self.search.text().strip()}”", "Clear the search",
                                    lambda: self.search.clear()))
        if m.query.order_by:
            order = ", ".join(f"{o.expr} {'↑' if o.direction == SortDirection.Ascending else '↓'}" for o in m.query.order_by)
            chips.append(self._chip(f"Sorted by {order}", "Remove sorting", lambda: m.sort_by([])))
        self.chips_bar.setVisible(bool(chips))
        if not chips:
            return
        self.chips_layout.addWidget(label("ACTIVE", "section"))
        for ch in chips:
            self.chips_layout.addWidget(ch)
        self.chips_layout.addStretch(1)
        clear = QToolButton()
        clear.setText("Clear all")
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self.clear_filters)
        self.chips_layout.addWidget(clear)

    # ------------------------------------------------------------------ selection, details and export
    def _update_selection_bar(self) -> None:
        rows, cells = self.grid.selection_summary()
        show = rows > 1 or (rows == 1 and self.grid.row_selection()) or cells > 1
        self.sel_bar.setVisible(show)
        if show:
            self.sel_label.setText(f"{rows:,} row{'s' if rows != 1 else ''} selected" if rows > 1 or self.grid.row_selection()
                                   else f"{cells:,} cells selected")
        editable = self.model.is_editable()
        self.btn_sel_null.setEnabled(editable)
        self.btn_sel_delete.setEnabled(editable)

    def _copy_rows(self, fmt: str) -> None:
        self.grid.copy_rows_as(fmt)
        rows, _ = self.grid.selection_summary()
        self.statusMessage.emit(f"Copied {rows:,} row{'s' if rows != 1 else ''} as {ROW_FORMATS[fmt]}")

    def show_row_details(self, row: int) -> None:
        from .dialogs.record_view import RecordDialog
        if 0 <= row < self.model.rowCount():
            if not self.model.row_available(row):
                self.model.complete_cache()
            RecordDialog(self.model, row, 1, self).exec()

    def export_selected(self) -> None:
        names, rows = self.grid.selected_rows_data()
        if rows:
            self._export_rows(names, rows, f"{self.ident.name}-selection")

    def export_page(self) -> None:
        names, rows = self.grid.view_rows_data()
        if rows:
            self._export_rows(names, rows, f"{self.ident.name}-page{self.model.page + 1}" if self.model.page_size
                              else self.ident.name)

    def _export_rows(self, names: list[str], rows: list[list], base: str) -> None:
        filters = "CSV (*.csv);;JSON (*.json);;SQL INSERT statements (*.sql);;Markdown table (*.md);;Tab separated (*.tsv)"
        path, chosen = QFileDialog.getSaveFileName(self, f"Export {len(rows):,} rows", f"{base}.csv", filters)
        if not path:
            return
        ext_by_filter = {"JSON": ".json", "SQL": ".sql", "Markdown": ".md", "Tab": ".tsv", "CSV": ".csv"}
        if "." not in os.path.basename(path):
            path += next((e for k, e in ext_by_filter.items() if chosen.startswith(k)), ".csv")
        fmt = format_for_path(path)
        text = format_rows(fmt, names, rows, self.ident.name,
                           sep=Settings.get("exportcsv", "separator"), quote=Settings.get("exportcsv", "quotecharacter"),
                           newline=Settings.get("exportcsv", "newlinecharacters"),
                           header=bool(Settings.get("exportcsv", "firstrowheader")),
                           pretty_json=bool(Settings.get("exportjson", "prettyprint")))
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except OSError as e:
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Couldn't write the file:\n{e}")
            return
        self.statusMessage.emit(f"Exported {len(rows):,} rows to {os.path.basename(path)}")

    def _on_error(self, msg: str) -> None:
        self.loading.hide()
        QMessageBox.warning(self, APP_DISPLAY_NAME, msg)

    # ------------------------------------------------------------------ editing
    def add_record(self) -> None:
        if not self.model.is_editable():
            return
        self.model.wait_until_idle()
        if self.model.insertRows(self.model.rowCount(), 1):
            r = self.model.rowCount() - 1
            self.grid.select_cell(r, 1)
            self.statusMessage.emit("Record added")

    def duplicate_record(self) -> None:
        rows = self.grid.selected_rows()
        if not rows or not self.model.is_editable():
            return
        self.model.wait_until_idle()
        ix = self.model.ditto_record(rows[0])
        if ix.isValid():
            self.grid.select_cell(ix.row(), ix.column())

    def delete_records(self) -> None:
        rows = self.grid.selected_rows()
        if not rows or not self.model.is_editable():
            return
        if len(rows) > 1 and QMessageBox.question(self, "Delete records", f"Delete {len(rows)} records?") != QMessageBox.Yes:
            return
        self.model.wait_until_idle()
        if self.model.remove_row_list(rows):
            self.statusMessage.emit(f"Deleted {len(rows)} record{'s' if len(rows) != 1 else ''}")
            self._update_footer()

    def jump_to_value(self, column: str, value) -> None:
        """Filter on ``column = value`` (used for foreign key navigation)."""
        self.clear_filters()
        from ..core.data import value_to_text
        self.set_filter(column, "=" + value_to_text(value))

    # ------------------------------------------------------------------ views: pseudo primary key
    def pseudo_pk(self) -> Optional[str]:
        """The column used as primary key for editing a view, or None if the view is read-only."""
        q = self.model.query
        return q.rowid_columns[0] if q.is_view and q.has_custom_rowid_column() else None

    def set_pseudo_pk(self, column: Optional[str]) -> None:
        """Make a view editable by addressing its rows through ``column`` (None locks it again).
        Port of ``TableBrowser::unlockViewEditing``."""
        t = self.table
        if t is None or not t.is_view() or column == self.pseudo_pk():
            return
        if column is not None and column not in t.field_names():
            return
        self.model.query.rowid_columns = [column] if column else ["_rowid_"]
        self.reload()
        self.statusMessage.emit(f"Editing enabled using '{column}' as primary key" if column else "View locked for editing")

    def _update_pk_button(self, is_view: bool) -> None:
        self.btn_pk.setVisible(is_view)
        self.btn_pk.setEnabled(is_view and not self.db.read_only)
        pk = self.pseudo_pk()
        self.btn_pk.setText(pk or "")
        self.btn_pk.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if pk else Qt.ToolButtonIconOnly)

    def _fill_pk_menu(self) -> None:
        self.pk_menu.clear()
        t = self.table
        if t is None:
            return
        group = QActionGroup(self.pk_menu)
        current = self.pseudo_pk()
        for name in [None] + t.field_names():
            a = self.pk_menu.addAction(name if name else "None (read-only)")
            a.setCheckable(True)
            a.setChecked(name == current)
            a.triggered.connect(lambda _c=False, n=name: self.set_pseudo_pk(n))
            group.addAction(a)
            if name is None:
                self.pk_menu.addSeparator()

    # ------------------------------------------------------------------ project settings
    def browse_settings(self) -> TableSettings:
        """Sort, filters, column widths and pseudo primary key, for saving in a project."""
        m = self.model
        fields = m.headers()[1:]
        s = TableSettings(unlock_view_pk=self.pseudo_pk() or "_rowid_")
        s.sort = [(fields.index(o.expr), int(o.direction)) for o in m.query.order_by if o.expr in fields]
        for c in range(1, m.columnCount()):
            if text := self.grid.filter_header.filter_text(c):
                s.filters[c - 1] = text
            if not self.grid.isColumnHidden(c):
                s.column_widths[c] = self.grid.columnWidth(c)
        s.global_filters = tokenize_global_filter(self.search.text())
        return s

    def apply_browse_settings(self, s: TableSettings) -> None:
        """Restore what ``browse_settings`` returned (port of ``TableBrowser::applySettings``)."""
        if s.unlock_view_pk and s.unlock_view_pk != "_rowid_":
            self.set_pseudo_pk(s.unlock_view_pk)
        fields = self.model.headers()[1:]
        self.clear_filters()
        for index, text in s.filters.items():
            if 0 <= index < len(fields):
                self.grid.filter_header.set_filter(index + 1, text)
                self.model.update_filter(index + 1, text)  # now rather than after the filter delay
        self.model.sort_by([OrderBy(fields[i], SortDirection(mode)) for i, mode in s.sort if 0 <= i < len(fields)])
        if s.global_filters:
            self.search.setText(" ".join(s.global_filters))
            self._search_timer.stop()
            self._apply_global_filter()
        if s.column_widths:
            self._first_load = False  # keep the stored widths instead of auto-sizing
            for c, w in s.column_widths.items():
                if 0 < c < self.model.columnCount() and w > 0:
                    self.grid.setColumnWidth(c, w)
        self._update_footer()

    def is_expanded(self) -> bool:
        return self.btn_expand.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.btn_expand.setChecked(on)
        self.btn_expand.setIcon(icon("minimize" if on else "maximize"))
        self.btn_expand.setToolTip("Exit full screen (Esc)" if on else "Show table full screen (F11, Esc to exit)")
        self.fullScreenRequested.emit(on)

    def shutdown(self) -> None:
        self.model.shutdown()
