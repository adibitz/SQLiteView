"""Export tables/views/query results to CSV or JSON. Port of upstream ``ExportDataDialog``."""
from __future__ import annotations

import base64
import json
import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog,
                               QVBoxLayout, QWidget)

from ...core import sqlb
from ...core.data import is_binary, value_to_text
from ...core.db import DBBrowserDB
from ...core.settings import Settings
from ...core.table_model import SqliteTableModel
from ..icons import icon
from ..widgets import SegmentedControl, label

SEPARATORS = [(",", ","), (";", ";"), ("Tab", "\t"), ("|", "|")]
QUOTES = [('"', '"'), ("'", "'"), ("None", "")]
NEWLINES = [("Windows (CRLF)", "\r\n"), ("Unix (LF)", "\n")]


def csv_field(v, sep: str, quote: str, newline: str) -> str:
    """CSV quoting rules from upstream: base64 for binary, quote when special chars occur."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)) and is_binary(v):
        return base64.b64encode(bytes(v)).decode("ascii")
    s = value_to_text(v)
    if not quote and newline in s:
        return '"' + s.replace('"', '""') + '"'
    if quote and any(ch in s for ch in (newline, sep, quote, "\n", "\r")):
        return quote + s.replace(quote, quote + quote) + quote
    return s


def json_value(v):
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode("ascii")
    return v


class ExportDataDialog(QDialog):
    def __init__(self, db: DBBrowserDB, fmt: str, ident: Optional[sqlb.ObjectIdentifier] = None,
                 model: Optional[SqliteTableModel] = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.model = model
        self.setWindowTitle("Export Data")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(12)
        lay.addWidget(label("Export Data", "title"))
        self.fmt = SegmentedControl([("CSV", "file"), ("JSON", "braces")])
        self.fmt.set_current(1 if fmt == "json" else 0)
        self.fmt.changed.connect(self._fmt_changed)
        row = QHBoxLayout()
        row.addWidget(self.fmt)
        row.addStretch(1)
        lay.addLayout(row)

        if model is None:
            lay.addWidget(label("TABLES AND VIEWS", "section"))
            self.objects = QListWidget()
            self.objects.setSelectionMode(QListWidget.ExtendedSelection)
            for schema, objmap in db.schemata.items():
                for name in sorted(objmap.tables, key=str.lower):
                    t = objmap.tables[name]
                    oid = sqlb.ObjectIdentifier(schema, name)
                    it = QListWidgetItem(icon("view" if t.is_view() else "table", "accent"), oid.to_display_string())
                    it.setData(Qt.UserRole, oid)
                    self.objects.addItem(it)
                    if ident is not None and oid == ident:
                        it.setSelected(True)
            if ident is None and self.objects.count():
                self.objects.item(0).setSelected(True)
            lay.addWidget(self.objects, 1)
            sel_row = QHBoxLayout()
            sel_row.addWidget(label("Select multiple to export one file per table into a folder.", "faint"))
            sel_row.addStretch(1)
            lay.addLayout(sel_row)
        else:
            self.objects = None
            lay.addWidget(label(f"Exporting the current query result ({model.real_row_count():,} rows).", "muted"))

        self.csv_box = QWidget()
        f = QFormLayout(self.csv_box)
        f.setContentsMargins(0, 0, 0, 0)
        self.header = QCheckBox("Column names in first line")
        self.header.setChecked(bool(Settings.get("exportcsv", "firstrowheader")))
        self.sep = QComboBox()
        for t, v in SEPARATORS:
            self.sep.addItem(t, v)
        self.sep.setCurrentIndex(max(0, self.sep.findData(Settings.get("exportcsv", "separator"))))
        self.quote = QComboBox()
        for t, v in QUOTES:
            self.quote.addItem(t, v)
        self.quote.setCurrentIndex(max(0, self.quote.findData(Settings.get("exportcsv", "quotecharacter"))))
        self.newline = QComboBox()
        for t, v in NEWLINES:
            self.newline.addItem(t, v)
        self.newline.setCurrentIndex(max(0, self.newline.findData(Settings.get("exportcsv", "newlinecharacters"))))
        f.addRow("", self.header)
        f.addRow("Separator", self.sep)
        f.addRow("Quote character", self.quote)
        f.addRow("New line", self.newline)
        lay.addWidget(self.csv_box)
        self.json_box = QWidget()
        jf = QFormLayout(self.json_box)
        jf.setContentsMargins(0, 0, 0, 0)
        self.pretty = QCheckBox("Pretty print")
        self.pretty.setChecked(bool(Settings.get("exportjson", "prettyprint")))
        jf.addRow("", self.pretty)
        lay.addWidget(self.json_box)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export…")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fmt_changed(self.fmt.current())

    def _fmt_changed(self, i: int) -> None:
        self.csv_box.setVisible(i == 0)
        self.json_box.setVisible(i == 1)

    @property
    def is_json(self) -> bool:
        return self.fmt.current() == 1

    def _save_settings(self) -> None:
        Settings.set("exportcsv", "firstrowheader", self.header.isChecked())
        Settings.set("exportcsv", "separator", self.sep.currentData())
        Settings.set("exportcsv", "quotecharacter", self.quote.currentData())
        Settings.set("exportcsv", "newlinecharacters", self.newline.currentData())
        Settings.set("exportjson", "prettyprint", self.pretty.isChecked())

    def accept(self) -> None:
        ext = "json" if self.is_json else "csv"
        filt = "JSON files (*.json);;All files (*)" if self.is_json else "CSV files (*.csv);;Text files (*.txt);;All files (*)"
        jobs: list[tuple[str, str]] = []  # (query, filename)
        if self.model is not None:
            path, _ = QFileDialog.getSaveFileName(self, "Export", f"result.{ext}", filt)
            if not path:
                return
            jobs.append((self.model.sql if self.model.query.table.is_empty() else self.model.query.build_query(False), path))
        else:
            sel = [it.data(Qt.UserRole) for it in self.objects.selectedItems()]
            if not sel:
                QMessageBox.information(self, "Export", "Select at least one table or view.")
                return
            if len(sel) == 1:
                path, _ = QFileDialog.getSaveFileName(self, "Export", f"{sel[0].name}.{ext}", filt)
                if not path:
                    return
                jobs.append((f"SELECT * FROM {sel[0].to_string()}", path))
            else:
                folder = QFileDialog.getExistingDirectory(self, "Choose a folder for the exported files")
                if not folder:
                    return
                for oid in sel:
                    jobs.append((f"SELECT * FROM {oid.to_string()}", os.path.join(folder, f"{oid.name}.{ext}")))
        self._save_settings()
        progress = QProgressDialog("Exporting…", "Cancel", 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(400)
        for query, path in jobs:
            try:
                ok = self._export(query, path, progress)
            except OSError as e:
                QMessageBox.warning(self, "Export", f"Error while writing the file '{path}': {e}")
                return
            if not ok:
                progress.close()
                if progress.wasCanceled():
                    return
                QMessageBox.warning(self, "Export", f"Export of '{os.path.basename(path)}' failed:\n{self.db.last_error}")
                return
        progress.close()
        if self.parent() is not None and hasattr(self.parent(), "notify"):
            self.parent().notify(f"Export completed ({len(jobs)} file{'s' if len(jobs) != 1 else ''})")
        super().accept()

    def _export(self, query: str, path: str, progress: QProgressDialog) -> bool:
        import sqlite3
        tmp = path + ".part"
        with self.db.get("exporting data") as conn:
            if conn is None:
                return False
            from ...core.db import _text_factory_fallback
            conn.text_factory = _text_factory_fallback
            try:
                cur = self.db.run(conn, query)
                names = [d[0] for d in cur.description or []]
                n = 0
                with open(tmp, "w", encoding="utf-8", newline="") as fh:
                    if self.is_json:
                        rows = []
                        for r in cur:
                            rows.append({k: json_value(v) for k, v in zip(names, r)})
                            n += 1
                            if n % 2000 == 0:
                                QApplication.processEvents()
                                if progress.wasCanceled():
                                    raise _Cancel()
                        json.dump(rows, fh, ensure_ascii=False, indent=4 if self.pretty.isChecked() else None)
                    else:
                        sep, quote, nl = self.sep.currentData(), self.quote.currentData(), self.newline.currentData()
                        if self.header.isChecked():
                            q = quote or ""
                            fh.write(sep.join(f"{q}{c.replace(q, q + q) if q else c}{q}" for c in names) + nl)
                        for r in cur:
                            fh.write(sep.join(csv_field(v, sep, quote, nl) for v in r) + nl)
                            n += 1
                            if n % 5000 == 0:
                                QApplication.processEvents()
                                if progress.wasCanceled():
                                    raise _Cancel()
            except _Cancel:
                os.remove(tmp)
                return False
            except sqlite3.Error as e:
                self.db.last_error = str(e)
                if os.path.exists(tmp):
                    os.remove(tmp)
                return False
            finally:
                conn.text_factory = str
        os.replace(tmp, path)
        return True


class _Cancel(Exception):
    pass
