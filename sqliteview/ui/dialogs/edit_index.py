"""Create index dialog. Port of upstream ``EditIndexDialog`` (creation path)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ...core import sqlb
from ...core.db import DBBrowserDB
from ..sql_editor import SqlEditor
from ..widgets import label


class EditIndexDialog(QDialog):
    def __init__(self, db: DBBrowserDB, table: Optional[sqlb.ObjectIdentifier], parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Create Index")
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Create Index", "title"))
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("idx_table_column")
        self.table = QComboBox()
        for schema, objmap in db.schemata.items():
            for name, t in sorted(objmap.tables.items(), key=lambda kv: kv[0].lower()):
                if t.is_view() or t.is_virtual() or name.startswith("sqlite_"):
                    continue
                oid = sqlb.ObjectIdentifier(schema, name)
                self.table.addItem(oid.to_display_string(), oid)
        if table is not None:
            i = self.table.findData(table)
            if i >= 0:
                self.table.setCurrentIndex(i)
        self.unique = QCheckBox("Unique")
        self.where = QLineEdit()
        self.where.setPlaceholderText("optional, e.g. deleted_at IS NULL (partial index)")
        form.addRow("Name", self.name)
        form.addRow("Table", self.table)
        form.addRow("", self.unique)
        form.addRow("Where", self.where)
        lay.addLayout(form)
        lay.addWidget(label("COLUMNS — check the columns to index; order follows the list", "section"))
        self.cols = QTableWidget(0, 2)
        self.cols.setHorizontalHeaderLabels(["Column", "Order"])
        self.cols.verticalHeader().hide()
        self.cols.horizontalHeader().setStretchLastSection(True)
        self.cols.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.cols, 1)
        lay.addWidget(label("SQL PREVIEW", "section"))
        self.preview = SqlEditor(read_only=True)
        self.preview.setMaximumHeight(110)
        lay.addWidget(self.preview)
        self.error = label("", "error", wrap=True)
        lay.addWidget(self.error)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Create Index")
        self.bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)
        self.table.currentIndexChanged.connect(self._fill_columns)
        for w in (self.name, self.where):
            w.textChanged.connect(self._update)
        self.unique.toggled.connect(self._update)
        self.cols.itemChanged.connect(self._update)
        self._fill_columns()

    def _fill_columns(self) -> None:
        oid = self.table.currentData()
        t = self.db.get_table_by_name(oid) if oid else None
        self.cols.blockSignals(True)
        self.cols.setRowCount(0)
        if t is not None:
            self.cols.setRowCount(len(t.fields))
            for r, f in enumerate(t.fields):
                it = QTableWidgetItem(f.name)
                it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                it.setCheckState(Qt.Unchecked)
                self.cols.setItem(r, 0, it)
                cb = QComboBox()
                cb.addItems(["", "ASC", "DESC"])
                cb.currentIndexChanged.connect(self._update)
                self.cols.setCellWidget(r, 1, cb)
            if not self.name.text():
                self.name.setPlaceholderText(f"idx_{t.name}")
        self.cols.blockSignals(False)
        self._update()

    def _index(self) -> sqlb.Index:
        oid = self.table.currentData()
        idx = sqlb.Index(self.name.text().strip())
        idx.table = oid.name if oid else ""
        idx.unique = self.unique.isChecked()
        idx.where_expr = self.where.text().strip()
        for r in range(self.cols.rowCount()):
            it = self.cols.item(r, 0)
            if it.checkState() == Qt.Checked:
                idx.fields.append(sqlb.IndexedColumn(it.text(), False, self.cols.cellWidget(r, 1).currentText()))
        return idx

    def _validate(self, idx: sqlb.Index) -> str:
        if not idx.name:
            return "Please enter a name for the index."
        oid = self.table.currentData()
        if oid is None:
            return "Please select a table."
        if self.db.get_index_by_name(sqlb.ObjectIdentifier(oid.schema, idx.name)):
            return f"An index named '{idx.name}' already exists."
        if not idx.fields:
            return "Please select at least one column."
        return ""

    def _update(self, *_) -> None:
        idx = self._index()
        err = self._validate(idx)
        self.error.setText(err)
        self.bb.button(QDialogButtonBox.Ok).setEnabled(not err)
        oid = self.table.currentData()
        self.preview.setPlainText(idx.sql(oid.schema if oid else "main") if idx.fields else "")

    def accept(self) -> None:
        idx = self._index()
        err = self._validate(idx)
        if err:
            self.error.setText(err)
            return
        oid = self.table.currentData()
        self.db.set_undo_savepoint()
        if not self.db.execute_sql(idx.sql(oid.schema)):
            self.error.setText(f"Creating the index failed:\n{self.db.last_error}")
            self.db.revert_to_undo_savepoint()
            return
        super().accept()
