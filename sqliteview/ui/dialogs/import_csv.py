"""Import CSV files into tables. Port of upstream ``ImportCsvDialog``; parsing, type detection
and the insert path live in ``core/csvimport.py``.
"""
from __future__ import annotations

import csv
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLineEdit, QListWidget, QMessageBox, QProgressDialog,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...core import sqlb
from ...core.csvimport import (CsvImportError, CsvOptions, ImportCancelled, detect_fields, empty_values_for,
                               import_csv_file, read_preview)
from ...core.db import DBBrowserDB
from ...core.settings import Settings
from ..icons import icon
from ..widgets import label

SEPARATORS = [(",", ","), (";", ";"), ("Tab", "\t"), ("|", "|"), ("Space", " ")]
QUOTES = [('"', '"'), ("'", "'"), ("None", "")]
ENCODINGS = ["UTF-8", "UTF-16", "ISO-8859-1", "Windows-1252", "Windows-1251", "Shift_JIS", "GB18030", "Big5"]
CONFLICT = [("Abort on conflict (default)", "abort"), ("Ignore conflicting rows", "ignore"),
            ("Replace conflicting rows", "replace")]


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

    def _options(self) -> CsvOptions:
        return CsvOptions(separator=self._separator(), quote=self.quote.currentData(),
                          encoding=self.encoding.currentText(), header=self.header.isChecked(),
                          trim=self.trim.isChecked(), sanitize_names=self.sanitize.isChecked(),
                          detect_types=not self.no_types.isChecked())

    # ------------------------------------------------------------------ preview
    def update_preview(self) -> None:
        path = self._current_file()
        self.error.setText("")
        try:
            opts = self._options()
            fields = detect_fields(path, opts)
            rows = read_preview(path, opts, 100)
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
        opts = self._options()
        fields = detect_fields(path, opts)
        if not fields:
            return True
        tbl = self.db.get_table_by_name(sqlb.ObjectIdentifier("main", table_name))
        empty_values = None
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
            empty_values = empty_values_for(self.db, tbl, use_defaults=not self.ignore_defaults.isChecked())

        progress = QProgressDialog(f"Importing {os.path.basename(path)}…", "Cancel", 0, 1000, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)

        def on_progress(permille: int) -> bool:
            progress.setValue(permille)
            QApplication.processEvents()
            return not progress.wasCanceled()

        try:
            import_csv_file(self.db, path, table_name, opts, fields, to_existing=tbl is not None,
                            conflict=self.conflict.currentData(), empty_values=empty_values, progress=on_progress)
        except ImportCancelled:
            return False
        except CsvImportError as e:
            QMessageBox.warning(self, "Import CSV", str(e))
            return False
        finally:
            progress.close()
        self._dont_ask.add(table_name)
        self.imported_tables.append(table_name)
        return True
