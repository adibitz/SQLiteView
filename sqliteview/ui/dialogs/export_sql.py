"""Export database to SQL. Port of upstream ``ExportSqlDialog`` (uses ``DBBrowserDB.dump``)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QVBoxLayout)

from ...core.db import DBBrowserDB
from ...core.settings import Settings
from ..icons import icon
from ..widgets import label, push_button

EXPORT_WHAT = ["Export everything", "Export schema only", "Export data only"]
OLD_SCHEMA = ["Keep old schema (CREATE TABLE IF NOT EXISTS)", "Overwrite old schema (DROP TABLE, then CREATE TABLE)"]


class ExportSqlDialog(QDialog):
    def __init__(self, db: DBBrowserDB, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Export SQL")
        self.resize(560, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Export Database to SQL", "title"))
        head = QHBoxLayout()
        head.addWidget(label("TABLES", "section"))
        head.addStretch(1)
        head.addWidget(push_button("Select all", kind="link", on_click=lambda: self._select(True)))
        head.addWidget(push_button("Deselect all", kind="link", on_click=lambda: self._select(False)))
        lay.addLayout(head)
        self.tables = QListWidget()
        for name, t in sorted(db.schemata.get("main").tables.items(), key=lambda kv: kv[0].lower()):
            if t.is_view() or name.startswith("sqlite_"):
                continue
            it = QListWidgetItem(icon("table", "accent"), name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.tables.addItem(it)
        lay.addWidget(self.tables, 1)
        form = QFormLayout()
        self.col_names = QCheckBox("Keep column names in INSERT INTO")
        self.col_names.setChecked(bool(Settings.get("exportsql", "insertcolnames")))
        self.multiple = QCheckBox("Multiple rows (VALUES) per INSERT statement")
        self.multiple.setChecked(bool(Settings.get("exportsql", "insertmultiple")))
        self.keep_original = QCheckBox("Keep original CREATE statements")
        self.keep_original.setChecked(bool(Settings.get("exportsql", "keeporiginal")))
        self.what = QComboBox()
        self.what.addItems(EXPORT_WHAT)
        self.old_schema = QComboBox()
        self.old_schema.addItems(OLD_SCHEMA)
        self.old_schema.setCurrentIndex(int(Settings.get("exportsql", "oldschema")))
        form.addRow("", self.col_names)
        form.addRow("", self.multiple)
        form.addRow("", self.keep_original)
        form.addRow("Content", self.what)
        form.addRow("Existing schema", self.old_schema)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export…")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _select(self, on: bool) -> None:
        for i in range(self.tables.count()):
            self.tables.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def accept(self) -> None:
        tables = [self.tables.item(i).text() for i in range(self.tables.count())
                  if self.tables.item(i).checkState() == Qt.Checked]
        if not tables:
            QMessageBox.information(self, "Export SQL", "Please select at least one table.")
            return
        base = self.db.cur_file if self.db.cur_file != ":memory:" else "database"
        import os
        path, _ = QFileDialog.getSaveFileName(self, "Export SQL", os.path.splitext(base)[0] + ".sql",
                                              "SQL files (*.sql);;Text files (*.txt);;All files (*)")
        if not path:
            return
        Settings.set("exportsql", "insertcolnames", self.col_names.isChecked())
        Settings.set("exportsql", "insertmultiple", self.multiple.isChecked())
        Settings.set("exportsql", "keeporiginal", self.keep_original.isChecked())
        Settings.set("exportsql", "oldschema", self.old_schema.currentIndex())
        what = self.what.currentIndex()
        progress = QProgressDialog("Exporting database to SQL file…", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)

        def cb(done: int, total: int) -> bool:
            progress.setMaximum(max(1, total))
            progress.setValue(done)
            QApplication.processEvents()
            return not progress.wasCanceled()

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok = self.db.dump(path, tables, self.col_names.isChecked(), self.multiple.isChecked(),
                              self.keep_original.isChecked(), what in (0, 1), what in (0, 2),
                              self.old_schema.currentIndex() == 0, cb)
        finally:
            QApplication.restoreOverrideCursor()
            progress.close()
        if not ok:
            if not progress.wasCanceled():
                QMessageBox.warning(self, "Export SQL", f"Export failed:\n{self.db.last_error}")
            return
        if self.parent() is not None and hasattr(self.parent(), "notify"):
            self.parent().notify("Export completed")
        super().accept()
