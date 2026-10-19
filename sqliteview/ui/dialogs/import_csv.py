"""Import CSV files into tables. Port of upstream ``ImportCsvDialog`` (+ ``CSVParser``).

Parsing uses Python's C-implemented ``csv`` module; the insert path uses ``executemany`` on a
single prepared statement inside a savepoint, the same strategy as the C++ code.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
from typing import Iterator, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLineEdit, QListWidget, QMessageBox, QProgressDialog,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...core import sqlb
from ...core.db import DBBrowserDB
from ...core.settings import Settings
from ..icons import icon
from ..widgets import label

SEPARATORS = [(",", ","), (";", ";"), ("Tab", "\t"), ("|", "|"), ("Space", " ")]
QUOTES = [('"', '"'), ("'", "'"), ("None", "")]
ENCODINGS = ["UTF-8", "UTF-16", "ISO-8859-1", "Windows-1252", "Windows-1251", "Shift_JIS", "GB18030", "Big5"]
CONFLICT = [("Abort on conflict (default)", ""), ("Ignore conflicting rows", "OR IGNORE"), ("Replace conflicting rows", "OR REPLACE")]
TYPE_DETECTION_ROWS = 20


class _CountingFile:
    """Wraps a text file to report how many characters have been consumed (for progress)."""

    def __init__(self, fh):
        self.fh = fh
        self.consumed = 0

    def __iter__(self) -> Iterator[str]:
        for line in self.fh:
            self.consumed += len(line)
            yield line


def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


class ImportCsvDialog(QDialog):
    def __init__(self, db: DBBrowserDB, files: list[str], parent=None):
        super().__init__(parent)
        self.db = db
        self.files = list(files)
        self.imported_tables: list[str] = []
        self.setWindowTitle("Import CSV")
        self.resize(980, 680)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self.update_preview)
        self._build()
        self.update_preview()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Import CSV", "title"))
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        form = QFormLayout(left)
        form.setContentsMargins(0, 0, 10, 0)
        form.setVerticalSpacing(8)
        if len(self.files) > 1:
            self.file_list = QListWidget()
            for f in self.files:
                self.file_list.addItem(os.path.basename(f))
            self.file_list.setCurrentRow(0)
            self.file_list.currentRowChanged.connect(lambda _r: self._on_file_changed())
            self.file_list.setMaximumHeight(110)
            form.addRow("Files", self.file_list)
        else:
            self.file_list = None
        self.table_name = QLineEdit(self._default_name(self.files[0]))
        self.table_name.textChanged.connect(self._schedule)
        form.addRow("Table name", self.table_name)
        self.header = QCheckBox("Column names in first line")
        self.header.setChecked(bool(Settings.get("importcsv", "firstrowheader")))
        form.addRow("", self.header)
        self.sep = QComboBox()
        for t, v in SEPARATORS:
            self.sep.addItem(t, v)
        self.sep.setEditable(True)
        sep_setting = Settings.get("importcsv", "separator")
        i = self.sep.findData(sep_setting)
        self.sep.setCurrentIndex(i if i >= 0 else 0)
        form.addRow("Separator", self.sep)
        self.quote = QComboBox()
        for t, v in QUOTES:
            self.quote.addItem(t, v)
        self.quote.setCurrentIndex(max(0, self.quote.findData(Settings.get("importcsv", "quotecharacter"))))
        form.addRow("Quote character", self.quote)
        self.encoding = QComboBox()
        self.encoding.setEditable(True)
        self.encoding.addItems(ENCODINGS)
        self.encoding.setCurrentText(Settings.get("importcsv", "encoding"))
        form.addRow("Encoding", self.encoding)
        self.trim = QCheckBox("Trim fields")
        self.trim.setChecked(bool(Settings.get("importcsv", "trimfields")))
        form.addRow("", self.trim)
        self.sanitize = QCheckBox("Replace non-alphanumeric characters in column names")
        form.addRow("", self.sanitize)
        self.no_types = QCheckBox("Disable data type detection")
        self.no_types.setChecked(bool(Settings.get("importcsv", "notypedetection")))
        form.addRow("", self.no_types)
        self.separate = QCheckBox("Separate table per file")
        self.separate.setChecked(bool(Settings.get("importcsv", "separatetables")))
        self.separate.setVisible(len(self.files) > 1)
        form.addRow("", self.separate)
        self.ignore_defaults = QCheckBox("Ignore default values (existing tables)")
        form.addRow("", self.ignore_defaults)
        self.conflict = QComboBox()
        for t, v in CONFLICT:
            self.conflict.addItem(t, v)
        form.addRow("On conflict", self.conflict)
        for w in (self.header, self.trim, self.sanitize, self.no_types):
            w.toggled.connect(self._schedule)
        self.sep.currentTextChanged.connect(self._schedule)
        self.quote.currentIndexChanged.connect(self._schedule)
        self.encoding.currentTextChanged.connect(self._schedule)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(label("PREVIEW", "section"))
        head.addStretch(1)
        self.target_chip = label("", "chip-accent")
        head.addWidget(self.target_chip)
        rl.addLayout(head)
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview.setAlternatingRowColors(True)
        self.preview.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        rl.addWidget(self.preview, 1)
        self.preview_info = label("", "faint")
        rl.addWidget(self.preview_info)
        split.addWidget(right)
        split.setSizes([340, 620])
        lay.addWidget(split, 1)
        self.error = label("", "error", wrap=True)
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Import")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _default_name(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    def _current_file(self) -> str:
        if self.file_list is not None and self.file_list.currentRow() >= 0:
            return self.files[self.file_list.currentRow()]
        return self.files[0]

    def _on_file_changed(self) -> None:
        if self.separate.isChecked():
            self.table_name.setText(self._default_name(self._current_file()))
        self._schedule()

    def _schedule(self) -> None:
        self._preview_timer.start(150)

    def _separator(self) -> str:
        data = self.sep.currentData()
        txt = self.sep.currentText()
        if data is not None and self.sep.itemText(self.sep.currentIndex()) == txt:
            return data
        return {"Tab": "\t", "\\t": "\t", "Space": " "}.get(txt, txt[:1] or ",")

    def _encoding(self) -> str:
        enc = self.encoding.currentText().strip() or "UTF-8"
        return "utf-8-sig" if enc.upper() in ("UTF-8", "UTF8") else enc

    def _reader(self, fh) -> Iterator[list[str]]:
        quote = self.quote.currentData()
        kwargs = dict(delimiter=self._separator(), doublequote=True, strict=False)
        if quote:
            kwargs.update(quotechar=quote, quoting=csv.QUOTE_MINIMAL)
        else:
            kwargs.update(quoting=csv.QUOTE_NONE, escapechar=None)
        reader = csv.reader(fh, **kwargs)
        trim = self.trim.isChecked()
        for row in reader:
            if not row:
                continue
            yield [c.strip() for c in row] if trim else row

    def _field_list(self, path: str) -> list[sqlb.Field]:
        """Port of ``generateFieldList``: names from the header, types from the first rows."""
        fields: list[sqlb.Field] = []
        with open(path, encoding=self._encoding(), newline="", errors="replace") as fh:
            for row_num, row in enumerate(self._reader(fh)):
                if row_num > TYPE_DETECTION_ROWS:
                    break
                for i in range(len(fields), len(row)):
                    name = ""
                    if row_num == 0 and self.header.isChecked():
                        name = row[i]
                        if self.sanitize.isChecked():
                            name = re.sub(r"[^\w]", "_", name)
                    if not name:
                        name = f"field{i + 1}"
                    # make names unique
                    base, n = name, 1
                    while any(f.name.lower() == name.lower() for f in fields):
                        n += 1
                        name = f"{base}_{n}"
                    fields.append(sqlb.Field(name, ""))
                if self.no_types.isChecked() or (row_num == 0 and self.header.isChecked()):
                    continue
                for i, content in enumerate(row):
                    old = fields[i].type
                    if old == "TEXT":
                        continue
                    if content == "":
                        continue
                    as_int, as_real = _is_int(content), _is_float(content)
                    new = "TEXT"
                    if old == "INTEGER" and not as_int and as_real:
                        new = "REAL"
                    elif not old and as_int:
                        new = "INTEGER"
                    elif not old and as_real:
                        new = "REAL"
                    elif old == "REAL" and as_real:
                        new = "REAL"
                    elif old == "INTEGER" and as_int:
                        new = "INTEGER"
                    fields[i].type = new
        for f in fields:
            if not f.type and not self.no_types.isChecked():
                f.type = "TEXT"
        return fields

    # ------------------------------------------------------------------ preview
    def update_preview(self) -> None:
        path = self._current_file()
        self.error.setText("")
        try:
            fields = self._field_list(path)
            rows = []
            with open(path, encoding=self._encoding(), newline="", errors="replace") as fh:
                for i, row in enumerate(self._reader(fh)):
                    if i == 0 and self.header.isChecked():
                        continue
                    rows.append(row)
                    if len(rows) >= 100:
                        break
        except (OSError, csv.Error, LookupError) as e:
            self.error.setText(f"Couldn't read the file: {e}")
            self.preview.clear()
            return
        self.preview.clear()
        self.preview.setColumnCount(len(fields))
        self.preview.setHorizontalHeaderLabels([f"{f.name}  ({f.type or 'any'})" for f in fields])
        self.preview.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row[: len(fields)]):
                self.preview.setItem(r, c, QTableWidgetItem(v))
        self.preview.resizeColumnsToContents()
        size = os.path.getsize(path)
        self.preview_info.setText(f"{os.path.basename(path)} · {size:,} bytes · showing first {len(rows)} rows · "
                                  f"{len(fields)} columns")
        name = self.table_name.text().strip()
        existing = self.db.get_table_by_name(sqlb.ObjectIdentifier("main", name)) if name else None
        if existing is not None:
            self.target_chip.setText(f"Append to existing table '{name}'")
        else:
            self.target_chip.setText(f"New table '{name}'" if name else "")

    # ------------------------------------------------------------------ import
    def _save_settings(self) -> None:
        Settings.set("importcsv", "firstrowheader", self.header.isChecked())
        Settings.set("importcsv", "trimfields", self.trim.isChecked())
        Settings.set("importcsv", "separatetables", self.separate.isChecked())
        Settings.set("importcsv", "separator", self._separator())
        Settings.set("importcsv", "quotecharacter", self.quote.currentData())
        Settings.set("importcsv", "encoding", self.encoding.currentText())
        Settings.set("importcsv", "notypedetection", self.no_types.isChecked())

    def accept(self) -> None:
        if not self.table_name.text().strip() and not (len(self.files) > 1 and self.separate.isChecked()):
            self.error.setText("Please enter a table name.")
            return
        self._save_settings()
        self._dont_ask: set[str] = set()
        for path in self.files:
            if len(self.files) > 1 and self.separate.isChecked():
                name = self._default_name(path)
            else:
                name = self.table_name.text().strip()
            if not self._import_file(path, name):
                return
        self.db.update_schema()
        super().accept()

    def _import_file(self, path: str, table_name: str) -> bool:
        """Returns False to abort the whole import (error or cancel)."""
        fields = self._field_list(path)
        if not fields:
            return True
        ident = sqlb.ObjectIdentifier("main", table_name)
        tbl = self.db.get_table_by_name(ident)
        to_existing = False
        null_values: list = []
        if tbl is not None:
            if len(tbl.fields) != len(fields):
                QMessageBox.warning(self, "Import CSV", f"There is already a table named '{table_name}' and an import "
                                    "into an existing table is only possible if the number of columns match.")
                return True
            if "" not in self._dont_ask and table_name not in self._dont_ask:
                r = QMessageBox.question(self, "Import CSV",
                                         f"There is already a table named '{table_name}'. Do you want to import the data into it?",
                                         QMessageBox.Yes | QMessageBox.No | QMessageBox.YesToAll | QMessageBox.Cancel,
                                         QMessageBox.No)
                if r == QMessageBox.No:
                    return True
                if r == QMessageBox.Cancel:
                    return False
                self._dont_ask.add("" if r == QMessageBox.YesToAll else table_name)
            to_existing = True
            for f in tbl.fields:
                if not self.ignore_defaults.isChecked() and f.default_value:
                    null_values.append(("default", f.default_value))
                elif f.notnull:
                    null_values.append(("value", 0 if f.is_integer() else ""))
                else:
                    null_values.append(("value", None))

        savepoint = self.db.generate_savepoint_name("csvimport")
        if not self.db.set_savepoint(savepoint):
            QMessageBox.warning(self, "Import CSV", f"Creating restore point failed: {self.db.last_error}")
            return False
        if not to_existing and not self.db.create_table(ident, fields):
            self.db.revert_to_savepoint(savepoint)
            QMessageBox.warning(self, "Import CSV", f"Creating the table failed: {self.db.last_error}")
            return False
        self._dont_ask.add(table_name)
        if to_existing:
            # defaults have to be evaluated by SQLite, so pre-compute them
            resolved = []
            for kind, v in null_values:
                if kind == "default":
                    resolved.append(self.db.query_single_value(f"SELECT {v}", log=False))
                else:
                    resolved.append(v)
            null_values = resolved
        n = len(fields)
        sql = f"INSERT {self.conflict.currentData()} INTO {sqlb.escape_identifier(table_name)} VALUES({','.join('?' * n)})"
        total = max(1, os.path.getsize(path))
        progress = QProgressDialog(f"Importing {os.path.basename(path)}…", "Cancel", 0, 1000, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)
        has_header = self.header.isChecked()
        rows_done = 0
        try:
            with open(path, encoding=self._encoding(), newline="", errors="replace") as raw, \
                    self.db.get("importing CSV") as conn:
                fh = _CountingFile(raw)
                reader = self._reader(fh)
                if has_header:
                    next(reader, None)
                batch: list[list] = []

                def flush() -> None:
                    conn.executemany(sql, batch)
                    batch.clear()

                for row in reader:
                    if len(row) < n:
                        row = row + [""] * (n - len(row))
                    elif len(row) > n:
                        row = row[:n]
                    if to_existing:
                        vals = [null_values[i] if v == "" else v for i, v in enumerate(row)]
                    else:
                        vals = [None if v == "" else v for v in row]
                    batch.append(vals)
                    if len(batch) >= 5000:
                        flush()
                        rows_done += 5000
                        progress.setValue(int(fh.consumed * 1000 / total))
                        QApplication.processEvents()
                        if progress.wasCanceled():
                            raise _Cancel()
                if batch:
                    rows_done += len(batch)
                    flush()
        except _Cancel:
            progress.close()
            self.db.revert_to_savepoint(savepoint)
            return False
        except (sqlite3.Error, csv.Error, OSError, UnicodeError) as e:
            progress.close()
            self.db.revert_to_savepoint(savepoint)
            QMessageBox.warning(self, "Import CSV", f"Inserting row failed (around row {rows_done + 1}):\n{e}")
            return False
        progress.close()
        self.db.log_sql(f"-- Imported {os.path.basename(path)} into {table_name}\n{sql}")
        self.imported_tables.append(table_name)
        return True


class _Cancel(Exception):
    pass
