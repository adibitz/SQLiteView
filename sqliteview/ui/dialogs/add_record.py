"""Insert a record with values. Port of upstream ``AddRecordDialog``."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ...core import sqlb
from ...core.db import DBBrowserDB, _expand_sql
from ..icons import icon
from ..sql_editor import SqlEditor
from ..widgets import label

C_NAME, C_TYPE, C_VALUE = range(3)


class AddRecordDialog(QDialog):
    def __init__(self, db: DBBrowserDB, ident: sqlb.ObjectIdentifier, parent=None):
        super().__init__(parent)
        self.db = db
        self.ident = ident
        self.table = db.get_table_by_name(ident)
        self.setWindowTitle(f"Insert Record — {ident.to_display_string()}")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label(f"New record in {ident.to_display_string()}", "title"))
        lay.addWidget(label("Leave a value empty to use the column's default (or NULL). Type NULL explicitly for NULL.",
                            "faint", wrap=True))
        self.grid = QTableWidget(0, 3)
        self.grid.setHorizontalHeaderLabels(["Column", "Type", "Value"])
        self.grid.verticalHeader().hide()
        self.grid.horizontalHeader().setSectionResizeMode(C_VALUE, QHeaderView.Stretch)
        self.grid.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setEditTriggers(QAbstractItemView.AllEditTriggers)
        pk = self.table.primary_key_names() if self.table else []
        fields = [f for f in (self.table.fields if self.table else []) if not f.generated]
        self.grid.setRowCount(len(fields))
        for r, f in enumerate(fields):
            name = QTableWidgetItem(icon("key", "warning") if f.name in pk else icon("column"), f.name)
            name.setFlags(Qt.ItemIsEnabled)
            tip = [f.type or "no type"]
            if f.notnull:
                tip.append("NOT NULL")
            if f.default_value:
                tip.append("DEFAULT " + f.default_value)
            if f.check:
                tip.append("CHECK " + f.check)
            name.setToolTip(" · ".join(tip))
            self.grid.setItem(r, C_NAME, name)
            ti = QTableWidgetItem(f.type)
            ti.setFlags(Qt.ItemIsEnabled)
            self.grid.setItem(r, C_TYPE, ti)
            vi = QTableWidgetItem("")
            vi.setToolTip(f"Default: {f.default_value}" if f.default_value else "")
            self.grid.setItem(r, C_VALUE, vi)
        self.grid.resizeColumnToContents(C_NAME)
        self.grid.itemChanged.connect(self._update)
        lay.addWidget(self.grid, 1)
        lay.addWidget(label("SQL PREVIEW", "section"))
        self.preview = SqlEditor(read_only=True)
        self.preview.setMaximumHeight(120)
        lay.addWidget(self.preview)
        self.error = label("", "error", wrap=True)
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Reset)
        bb.button(QDialogButtonBox.Ok).setText("Insert")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.button(QDialogButtonBox.Reset).clicked.connect(self._reset)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fields = fields
        self._update()

    def _reset(self) -> None:
        for r in range(self.grid.rowCount()):
            self.grid.item(r, C_VALUE).setText("")

    def _statement(self) -> tuple[str, list]:
        cols, vals = [], []
        for r, f in enumerate(self._fields):
            text = self.grid.item(r, C_VALUE).text()
            if text == "":
                continue
            cols.append(f.name)
            if text.upper() == "NULL":
                vals.append(None)
            elif f.is_integer():
                try:
                    vals.append(int(text))
                except ValueError:
                    vals.append(text)
            elif f.is_real() or f.is_numeric():
                try:
                    vals.append(float(text))
                except ValueError:
                    vals.append(text)
            else:
                vals.append(text)
        target = self.ident.to_string()
        if not cols:
            return f"INSERT INTO {target} DEFAULT VALUES;", []
        return (f"INSERT INTO {target} ({', '.join(sqlb.escape_identifiers(cols))}) "
                f"VALUES ({', '.join('?' * len(cols))});", vals)

    def _update(self, *_) -> None:
        sql, params = self._statement()
        self.preview.setPlainText(_expand_sql(sql, params))

    def accept(self) -> None:
        sql, params = self._statement()
        self.db.set_undo_savepoint()
        self.db.set_savepoint()
        with self.db.exclusive("inserting record") as conn:
            try:
                self.db.log_sql(_expand_sql(sql, params))
                self.db.run(conn, sql, params)
            except sqlite3.Error as e:
                self.error.setText(f"Adding the record failed:\n{e}")
                return
        super().accept()
