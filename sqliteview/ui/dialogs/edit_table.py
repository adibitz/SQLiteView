"""Create / modify table dialog. Port of upstream ``EditTableDialog``.

Edits operate on a ``sqlb.Table`` copy and a list of (old, new) column name pairs, which is
then applied with ``DBBrowserDB.alter_table`` (or a CREATE TABLE for new tables) exactly like
the original. The generated SQL is previewed live.
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QHeaderView, QInputDialog, QLineEdit, QMessageBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ...core import parser, sqlb
from ...core.db import DATATYPES, DATATYPES_STRICT, DBBrowserDB, TrackColumns
from ..icons import icon
from ..sql_editor import SqlEditor
from ..widgets import label, tool_button

C_NAME, C_TYPE, C_NN, C_PK, C_AI, C_U, C_DEFAULT, C_CHECK, C_COLL, C_FK = range(10)
HEADERS = ["Name", "Type", "NN", "PK", "AI", "U", "Default", "Check", "Collation", "Foreign Key"]
HEADER_TIPS = ["Column name", "Data type", "Not null", "Primary key", "Autoincrement", "Unique", "Default value",
               "Check constraint", "Collation", "Foreign key: table(column) [ON DELETE …]"]


def parse_fk(text: str) -> Optional[sqlb.ForeignKeyClause]:
    text = text.strip()
    if not text:
        return None
    try:
        p = parser._Parser("REFERENCES " + text)
        fk = p.foreign_key_clause()
        if p.cur.kind != parser.EOF:
            return None
        return fk
    except (parser.ParseError, IndexError):
        return None


class EditTableDialog(QDialog):
    def __init__(self, db: DBBrowserDB, ident: Optional[sqlb.ObjectIdentifier], parent=None):
        super().__init__(parent)
        self.db = db
        self.ident = ident
        self.is_new = ident is None
        self.result_name = ""
        self.result_schema = ident.schema if ident else "main"
        if self.is_new:
            self.table = sqlb.Table("")
            self.track: TrackColumns = []
        else:
            orig = db.get_table_by_name(ident)
            self.table = orig.clone()
            self.track = [(f.name, f.name) for f in self.table.fields]
            if not orig.fully_parsed:
                QMessageBox.information(
                    self, "Modify table",
                    "This table's CREATE statement couldn't be fully parsed. Changes may lose constraints that "
                    "aren't shown here; check the SQL preview before saving.")
        self._updating = False
        self.setWindowTitle("Create Table" if self.is_new else f"Modify Table — {ident.to_display_string()}")
        self.resize(1080, 720)
        self._build()
        self._populate()
        self._update_sql()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Table", "muted"))
        self.name_edit = QLineEdit(self.table.name)
        self.name_edit.setPlaceholderText("table_name")
        self.name_edit.textChanged.connect(self._on_name)
        top.addWidget(self.name_edit, 2)
        self.schema_combo = QComboBox()
        self.schema_combo.addItems([s for s in self.db.schemata if s != "temp"] + ["temp"])
        self.schema_combo.setCurrentText(self.result_schema)
        self.schema_combo.currentTextChanged.connect(lambda _s: self._update_sql())
        top.addWidget(label("Schema", "muted"))
        top.addWidget(self.schema_combo)
        self.without_rowid = QCheckBox("WITHOUT ROWID")
        self.without_rowid.setToolTip("Store the table as a clustered index on its primary key")
        self.without_rowid.setChecked(self.table.without_rowid)
        self.without_rowid.toggled.connect(self._on_options)
        self.strict = QCheckBox("STRICT")
        self.strict.setToolTip("Enforce column data types (SQLite 3.37+)")
        self.strict.setChecked(self.table.strict)
        self.strict.toggled.connect(self._on_options)
        top.addWidget(self.without_rowid)
        top.addWidget(self.strict)
        lay.addLayout(top)

        split = QSplitter(Qt.Vertical)
        tabs = QTabWidget()
        # ---- columns
        cols_page = QWidget()
        cl = QVBoxLayout(cols_page)
        cl.setContentsMargins(0, 8, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(tool_button("plus", "Add column", text="Add", on_click=self.add_field))
        self.btn_remove = tool_button("minus", "Remove column", text="Remove", on_click=self.remove_field)
        self.btn_up = tool_button("arrow-up", "Move up", on_click=lambda: self.move_field(-1))
        self.btn_down = tool_button("arrow-down", "Move down", on_click=lambda: self.move_field(1))
        for b in (self.btn_remove, self.btn_up, self.btn_down):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(label("Double-click a cell to edit · NN not null · PK primary key · AI autoincrement · U unique", "faint"))
        cl.addLayout(bar)
        self.grid = QTableWidget(0, len(HEADERS))
        self.grid.setHorizontalHeaderLabels(HEADERS)
        for i, tip in enumerate(HEADER_TIPS):
            self.grid.horizontalHeaderItem(i).setToolTip(tip)
        self.grid.verticalHeader().setSectionsMovable(False)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setAlternatingRowColors(True)
        hh = self.grid.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for c, w in ((C_NAME, 170), (C_TYPE, 130), (C_NN, 40), (C_PK, 40), (C_AI, 40), (C_U, 40), (C_DEFAULT, 120),
                     (C_CHECK, 140), (C_COLL, 90)):
            self.grid.setColumnWidth(c, w)
        hh.setStretchLastSection(True)
        self.grid.itemChanged.connect(self._on_item_changed)
        self.grid.itemSelectionChanged.connect(self._update_buttons)
        cl.addWidget(self.grid, 1)
        tabs.addTab(cols_page, icon("column"), "Columns")
        # ---- constraints
        cons_page = QWidget()
        kl = QVBoxLayout(cons_page)
        kl.setContentsMargins(0, 8, 0, 0)
        kbar = QHBoxLayout()
        add_unique = tool_button("plus", "Add UNIQUE constraint", text="Unique", on_click=self.add_unique)
        add_check = tool_button("plus", "Add CHECK constraint", text="Check", on_click=self.add_check)
        add_fk = tool_button("plus", "Add multi-column foreign key", text="Foreign key", on_click=self.add_foreign_key)
        self.btn_remove_cons = tool_button("minus", "Remove constraint", text="Remove", on_click=self.remove_constraint)
        for b in (add_unique, add_check, add_fk, self.btn_remove_cons):
            kbar.addWidget(b)
        kbar.addStretch(1)
        kl.addLayout(kbar)
        self.cons = QTableWidget(0, 4)
        self.cons.setHorizontalHeaderLabels(["Type", "Columns", "Name", "SQL"])
        self.cons.horizontalHeader().setStretchLastSection(True)
        self.cons.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.cons.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cons.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.cons.verticalHeader().hide()
        kl.addWidget(self.cons, 1)
        tabs.addTab(cons_page, icon("key"), "Constraints")
        split.addWidget(tabs)

        prev = QWidget()
        pl = QVBoxLayout(prev)
        pl.setContentsMargins(0, 6, 0, 0)
        pl.addWidget(label("SQL PREVIEW", "section"))
        self.preview = SqlEditor(read_only=True)
        frame = QFrame()
        frame.setProperty("role", "card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(1, 1, 1, 1)
        fl.addWidget(self.preview)
        pl.addWidget(frame, 1)
        split.addWidget(prev)
        split.setSizes([430, 200])
        lay.addWidget(split, 1)

        self.error = label("", "error", wrap=True)
        lay.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create Table" if self.is_new else "Save Changes")
        self.buttons.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        if self.is_new:
            self.name_edit.setFocus()

    # ------------------------------------------------------------------ populate
    def _type_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        cb.setEditable(True)
        cb.addItems(DATATYPES_STRICT if self.table.strict else DATATYPES)
        cb.setCurrentText(current)
        cb.currentTextChanged.connect(self._on_type_changed)
        return cb

    @staticmethod
    def _check_item(checked: bool, enabled: bool = True) -> QTableWidgetItem:
        it = QTableWidgetItem()
        flags = Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
        if enabled:
            flags |= Qt.ItemIsEnabled
        it.setFlags(flags)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        return it

    def _populate(self) -> None:
        self._updating = True
        t = self.table
        self.grid.setRowCount(len(t.fields))
        pk_names = t.primary_key_names()
        pk = t.primary_key()
        for r, f in enumerate(t.fields):
            self.grid.setItem(r, C_NAME, QTableWidgetItem(f.name))
            self.grid.setCellWidget(r, C_TYPE, self._type_combo(f.type))
            self.grid.setItem(r, C_NN, self._check_item(f.notnull))
            self.grid.setItem(r, C_PK, self._check_item(f.name in pk_names))
            ai_ok = len(pk_names) == 1 and pk_names[0] == f.name and f.is_integer() and f.type.upper() == "INTEGER"
            self.grid.setItem(r, C_AI, self._check_item(bool(pk and pk.auto_increment and f.name in pk_names), ai_ok))
            self.grid.setItem(r, C_U, self._check_item(f.unique))
            self.grid.setItem(r, C_DEFAULT, QTableWidgetItem(f.default_value))
            self.grid.setItem(r, C_CHECK, QTableWidgetItem(f.check))
            self.grid.setItem(r, C_COLL, QTableWidgetItem(f.collation))
            fk = t.foreign_key([f.name])
            self.grid.setItem(r, C_FK, QTableWidgetItem(fk.to_string() if fk else ""))
            if f.generated:
                gi = self.grid.item(r, C_DEFAULT)
                gi.setText(f"AS ({f.generated.expression})")
                gi.setFlags(gi.flags() & ~Qt.ItemIsEditable)
                gi.setToolTip("Generated column (edit in SQL to change)")
            for c in (C_NN, C_PK, C_AI, C_U):
                self.grid.item(r, c).setTextAlignment(Qt.AlignCenter)
        self._populate_constraints()
        self._updating = False
        self._update_buttons()

    def _populate_constraints(self) -> None:
        t = self.table
        rows = []
        for cols, c in t.index_constraints:
            if c.is_primary_key() and len(cols) == 1 and not c.name and not c.conflict_action:
                kind = "PRIMARY KEY"
            else:
                kind = "PRIMARY KEY" if c.is_primary_key() else "UNIQUE"
            rows.append((kind, ", ".join(x.name for x in cols), c.name, c.to_sql(cols), c))
        for cols, fk in t.foreign_keys:
            rows.append(("FOREIGN KEY", ", ".join(cols), fk.name, fk.to_sql(cols), fk))
        for c in t.check_constraints:
            rows.append(("CHECK", "", c.name, c.to_sql(), c))
        self.cons.setRowCount(len(rows))
        self._cons_objects = []
        for r, (kind, cols, name, sql, obj) in enumerate(rows):
            for col, v in enumerate((kind, cols, name, sql)):
                self.cons.setItem(r, col, QTableWidgetItem(v))
            self._cons_objects.append(obj)
        self.cons.resizeColumnsToContents()

    # ------------------------------------------------------------------ edits
    def _row_of_widget(self, w: QWidget) -> int:
        for r in range(self.grid.rowCount()):
            if self.grid.cellWidget(r, C_TYPE) is w:
                return r
        return -1

    def _on_type_changed(self, text: str) -> None:
        r = self._row_of_widget(self.sender())
        if r < 0 or self._updating:
            return
        self.table.fields[r].type = text.strip()
        self._refresh_ai_enabled()
        self._update_sql()

    def _on_name(self, text: str) -> None:
        self.table.name = text.strip()
        self._update_sql()

    def _on_options(self) -> None:
        self.table.without_rowid = self.without_rowid.isChecked()
        self.table.strict = self.strict.isChecked()
        for r in range(self.grid.rowCount()):
            cb: QComboBox = self.grid.cellWidget(r, C_TYPE)
            cur = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(DATATYPES_STRICT if self.table.strict else DATATYPES)
            cb.setCurrentText(cur)
            cb.blockSignals(False)
        self._update_sql()

    def _rename_field(self, r: int, new: str) -> None:
        f = self.table.fields[r]
        old = f.name
        if not new or new == old:
            return
        if any(x.name.lower() == new.lower() for i, x in enumerate(self.table.fields) if i != r):
            self._show_error(f"There already is a column named '{new}'.")
            self._updating = True
            self.grid.item(r, C_NAME).setText(old)
            self._updating = False
            return
        f.name = new
        self.table.rename_key_in_all_constraints(old, new)
        self.track = [(o, new if n == old else n) for o, n in self.track]

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        r, c = item.row(), item.column()
        f = self.table.fields[r]
        checked = item.checkState() == Qt.Checked
        if c == C_NAME:
            self._rename_field(r, item.text().strip())
        elif c == C_NN:
            f.set_notnull(checked)
        elif c == C_U:
            f.set_unique(checked)
        elif c == C_DEFAULT:
            if not f.generated:
                f.set_default(item.text().strip())
        elif c == C_CHECK:
            f.set_check(item.text().strip())
        elif c == C_COLL:
            f.set_collation(item.text().strip())
        elif c == C_FK:
            text = item.text().strip()
            existing = self.table.foreign_key([f.name])
            if existing is not None:
                self.table.remove_constraint(existing)
            if text:
                fk = parse_fk(text)
                if fk is None:
                    self._show_error("Foreign key must look like: table(column) [ON DELETE CASCADE …]")
                else:
                    self.table.add_foreign_key([f.name], fk)
        elif c in (C_PK, C_AI):
            self._apply_pk()
        self._populate_constraints()
        self._refresh_ai_enabled()
        self._update_sql()

    def _apply_pk(self) -> None:
        names = [self.table.fields[r].name for r in range(self.grid.rowCount())
                 if self.grid.item(r, C_PK).checkState() == Qt.Checked]
        ai = any(self.grid.item(r, C_AI).checkState() == Qt.Checked for r in range(self.grid.rowCount()))
        ai = ai and len(names) == 1
        self.table.set_primary_key_columns(names, ai)
        if ai:
            # AUTOINCREMENT implies INTEGER PRIMARY KEY
            idx = self.table.field_index(names[0])
            f = self.table.fields[idx]
            if f.type.upper() != "INTEGER":
                f.type = "INTEGER"
                self._updating = True
                self.grid.cellWidget(idx, C_TYPE).setCurrentText("INTEGER")
                self._updating = False

    def _refresh_ai_enabled(self) -> None:
        self._updating = True
        pk = self.table.primary_key_names()
        for r, f in enumerate(self.table.fields):
            it = self.grid.item(r, C_AI)
            ok = len(pk) == 1 and pk[0] == f.name and f.type.upper() == "INTEGER"
            flags = Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | (Qt.ItemIsEnabled if ok else Qt.NoItemFlags)
            it.setFlags(flags)
            if not ok and it.checkState() == Qt.Checked:
                it.setCheckState(Qt.Unchecked)
                p = self.table.primary_key()
                if p:
                    p.auto_increment = False
        self._updating = False

    def add_field(self) -> None:
        n = len(self.table.fields) + 1
        names = {f.name.lower() for f in self.table.fields}
        name = f"Field{n}"
        while name.lower() in names:
            n += 1
            name = f"Field{n}"
        self.table.fields.append(sqlb.Field(name, "INTEGER"))
        self.track.append((None, name))
        self._populate()
        r = self.grid.rowCount() - 1
        self.grid.setCurrentCell(r, C_NAME)
        self.grid.editItem(self.grid.item(r, C_NAME))
        self._update_sql()

    def remove_field(self) -> None:
        r = self.grid.currentRow()
        if r < 0:
            return
        name = self.table.fields[r].name
        if not self.is_new and len(self.table.fields) == 1:
            self._show_error("A table needs at least one column.")
            return
        new_track = []
        for o, n in self.track:
            if n == name:
                if o is not None:
                    new_track.append((o, None))
            else:
                new_track.append((o, n))
        self.track = new_track
        self.table.remove_key_from_all_constraints(name)
        del self.table.fields[r]
        self._populate()
        self.grid.setCurrentCell(min(r, self.grid.rowCount() - 1), C_NAME)
        self._update_sql()

    def move_field(self, delta: int) -> None:
        r = self.grid.currentRow()
        t = r + delta
        if r < 0 or not (0 <= t < len(self.table.fields)):
            return
        f = self.table.fields
        f[r], f[t] = f[t], f[r]
        self._populate()
        self.grid.setCurrentCell(t, C_NAME)
        self._update_sql()

    def _update_buttons(self) -> None:
        r = self.grid.currentRow()
        self.btn_remove.setEnabled(r >= 0)
        self.btn_up.setEnabled(r > 0)
        self.btn_down.setEnabled(0 <= r < self.grid.rowCount() - 1)

    # ------------------------------------------------------------------ constraints
    def _pick_columns(self, title: str) -> Optional[list[str]]:
        names = self.table.field_names()
        if not names:
            return None
        text, ok = QInputDialog.getText(self, title, f"Columns (comma separated). Available: {', '.join(names)}")
        if not ok:
            return None
        cols = [c.strip().strip('"') for c in text.split(",") if c.strip()]
        missing = [c for c in cols if c not in names]
        if missing or not cols:
            self._show_error(f"Unknown column(s): {', '.join(missing) or '(none)'}")
            return None
        return cols

    def add_unique(self) -> None:
        cols = self._pick_columns("Add UNIQUE constraint")
        if cols:
            self.table.add_index_constraint(cols, sqlb.UniqueConstraint())
            self._populate_constraints()
            self._update_sql()

    def add_check(self) -> None:
        expr, ok = QInputDialog.getText(self, "Add CHECK constraint", "Expression, e.g. price > 0")
        if ok and expr.strip():
            self.table.check_constraints.append(sqlb.CheckConstraint(expr.strip()))
            self._populate_constraints()
            self._update_sql()

    def add_foreign_key(self) -> None:
        cols = self._pick_columns("Add foreign key")
        if not cols:
            return
        ref, ok = QInputDialog.getText(self, "Add foreign key", "References, e.g. other_table(a, b) ON DELETE CASCADE")
        fk = parse_fk(ref) if ok else None
        if fk is None:
            if ok:
                self._show_error("Couldn't parse the foreign key reference.")
            return
        self.table.add_foreign_key(cols, fk)
        self._populate()
        self._update_sql()

    def remove_constraint(self) -> None:
        r = self.cons.currentRow()
        if r < 0:
            return
        self.table.remove_constraint(self._cons_objects[r])
        self._populate()
        self._update_sql()

    # ------------------------------------------------------------------ preview / accept
    def _show_error(self, msg: str) -> None:
        self.error.setText(msg)

    def _validate(self) -> str:
        if not self.table.name:
            return "Please enter a name for the table."
        if not self.table.fields:
            return "Please add at least one column."
        seen = set()
        for f in self.table.fields:
            if not f.name:
                return "Column names can't be empty."
            if f.name.lower() in seen:
                return f"Duplicate column name '{f.name}'."
            seen.add(f.name.lower())
        if self.table.without_rowid and not self.table.primary_key_names():
            return "WITHOUT ROWID tables need a primary key."
        if self.table.without_rowid and self.table.primary_key() and self.table.primary_key().auto_increment:
            return "AUTOINCREMENT isn't allowed in WITHOUT ROWID tables."
        schema = self.schema_combo.currentText()
        existing = self.db.get_table_by_name(sqlb.ObjectIdentifier(schema, self.table.name))
        if existing is not None and (self.is_new or self.ident.name.lower() != self.table.name.lower() or self.ident.schema != schema):
            return f"A table named '{self.table.name}' already exists."
        return ""

    def _update_sql(self) -> None:
        err = self._validate()
        self.error.setText(err)
        ok_btn = self.buttons.button(QDialogButtonBox.Ok) if hasattr(self, "buttons") else None
        if ok_btn is not None:
            ok_btn.setEnabled(not err and not self.db.read_only)
        t = self.table
        sql = t.sql(self.schema_combo.currentText() if hasattr(self, "schema_combo") else "main") if t.fields else ""
        if hasattr(self, "preview"):
            self.preview.setPlainText(sql)

    def accept(self) -> None:
        err = self._validate()
        if err:
            self._show_error(err)
            return
        schema = self.schema_combo.currentText()
        if self.is_new:
            self.db.set_undo_savepoint()
            if not self.db.execute_sql(self.table.sql(schema)):
                self._show_error(f"Error creating table:\n{self.db.last_error}")
                self.db.revert_to_undo_savepoint()
                return
        else:
            self.db.set_undo_savepoint()
            ok = self.db.alter_table(self.ident, self.table, self.track,
                                     schema if schema != self.ident.schema else "")
            if not ok:
                self._show_error(f"Error modifying table:\n{self.db.last_error}")
                self.db.revert_to_undo_savepoint()
                self.db.update_schema()
                return
        self.result_name = self.table.name
        self.result_schema = schema
        super().accept()
