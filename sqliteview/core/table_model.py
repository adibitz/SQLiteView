"""Lazily loading table model. Port of upstream ``src/sqlitetablemodel.{h,cpp}``.

Column 0 always holds the rowid (or composite primary key) used to address records; the
views hide it. Rows are fetched by ``RowLoader`` in chunks of ``db/prefetchsize`` rows.
"""
from __future__ import annotations

import threading
from enum import IntEnum
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPixmap

from . import sqlb
from .condformat import CondFormat, CondFormatMatcher, add_cond_format
from .data import image_format, is_binary, remove_comments_from_query, value_to_text
from .db import DBBrowserDB
from .filters import filter_to_sql_condition
from .query import OrderBy, Query, SelectedColumn, SortDirection
from .rowloader import RowCache, RowLoader
from .settings import Settings

_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")

# Data role carrying the raw SQLite value (None / int / float / str / bytes)
RawValueRole = Qt.UserRole + 1


class RowCount(IntEnum):
    Unknown = 0
    Partial = 1
    Complete = 2


def readable_text_color(background: QColor) -> QColor:
    """Near-black or near-white, whichever contrasts more with ``background`` (WCAG relative luminance)."""
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * channel(background.redF()) + 0.7152 * channel(background.greenF()) + 0.0722 * channel(background.blueF())
    return QColor("#1d2127") if lum > 0.179 else QColor("#f1f3f8")


class CellColors:
    """Colour scheme for special values; set by the theme."""

    def __init__(self) -> None:
        self.reg_fg = QColor()
        self.reg_bg = QColor()
        self.null_fg = QColor("#9aa0a6")
        self.null_bg = QColor()
        self.bin_fg = QColor("#9aa0a6")
        self.bin_bg = QColor()
        self.loading_fg = QColor("#9aa0a6")
        self.fmt_fg = QColor()  # cells shown through a display format
        self.fmt_bg = QColor()


class SqliteTableModel(QAbstractTableModel):
    finishedFetch = Signal(int, int)
    finishedRowCount = Signal()
    errorOccurred = Signal(str)

    colors = CellColors()

    def __init__(self, db: DBBrowserDB, parent=None, force_wait: bool = False):
        super().__init__(parent)
        self.db = db
        self._life = 0
        self._current_row_count = 0
        self._real_row_count = 0
        self._row_count_available = RowCount.Unknown
        self._headers: list[str] = []
        self._data_types: list[sqlb.Affinity] = []
        self._query = Query()
        self._s_query = ""
        self._table: Optional[sqlb.Table] = None
        self._static = False
        self.last_error = ""
        # Conditional formats by column name, so they survive refreshes and schema reloads
        self._cond_formats: dict[str, list[CondFormat]] = {}
        self._matcher = CondFormatMatcher()
        self._cache = RowCache()
        self._cache_lock = threading.Lock()
        self.reload_settings()
        self._worker = RowLoader(db, self._cache, self._cache_lock, force_wait)
        self._worker.fetched.connect(self._handle_finished_fetch, Qt.QueuedConnection)
        self._worker.rowCountComplete.connect(self._handle_row_count_complete, Qt.QueuedConnection)
        self._worker.error.connect(self._handle_error, Qt.QueuedConnection)
        self._worker.start()

    def shutdown(self) -> None:
        self._worker.stop()
        self._worker.wait(3000)

    # ------------------------------------------------------------------ settings
    def reload_settings(self) -> None:
        self.null_text = Settings.get("databrowser", "null_text")
        self.blob_text = Settings.get("databrowser", "blob_text")
        self.symbol_limit = int(Settings.get("databrowser", "symbol_limit"))
        self.rows_limit = int(Settings.get("databrowser", "rows_limit"))
        self.image_preview = bool(Settings.get("databrowser", "image_preview"))
        self.chunk_size = max(100, int(Settings.get("db", "prefetchsize")))
        self.filter_escape = Settings.get("databrowser", "filter_escape")
        family = Settings.get("databrowser", "font")
        f = QFont(family) if family else QFont()
        size = int(Settings.get("databrowser", "fontsize") or 0)
        if size > 0:
            f.setPointSize(size)
        self.font = f

    # ------------------------------------------------------------------ fetching
    def _handle_finished_fetch(self, life: int, begin: int, end: int) -> None:
        if life < self._life:
            return
        old = self._current_row_count
        new = max(old, begin, end)
        if new != old:
            self.beginInsertRows(QModelIndex(), old, new - 1)
            self._current_row_count = new
            self.endInsertRows()
        if end != begin and self._headers:
            self.dataChanged.emit(self.index(begin, 0), self.index(end - 1, len(self._headers) - 1))
        if self._row_count_available != RowCount.Complete:
            self._row_count_available = RowCount.Partial
        self.finishedFetch.emit(begin, end)

    def _handle_row_count_complete(self, life: int, n: int) -> None:
        if life < self._life:
            return
        self._real_row_count = n
        n = min(n, self.rows_limit)
        self._row_count_available = RowCount.Complete
        self._handle_finished_fetch(life, n, n)
        self.finishedRowCount.emit()

    def _handle_error(self, life: int, msg: str) -> None:
        if life < self._life:
            return
        self.last_error = msg
        self.errorOccurred.emit(msg)

    def row_count_available(self) -> RowCount:
        return self._row_count_available

    def real_row_count(self) -> int:
        return self._real_row_count

    def reading_data(self) -> bool:
        return self._worker.reading_data()

    def wait_until_idle(self) -> None:
        self._worker.wait_until_idle()

    def _clear_cache(self) -> None:
        self._life += 1
        self._worker.cancel()
        self._worker.wait_until_idle()
        self.beginResetModel()
        with self._cache_lock:
            self._cache.clear()
        self._current_row_count = 0
        self._real_row_count = 0
        self._row_count_available = RowCount.Unknown
        self.endResetModel()

    def trigger_cache_load(self, row: int) -> None:
        if self._static:
            return
        half = self.chunk_size // 2
        begin = max(0, row - half)
        end = row + half
        if self._row_count_available == RowCount.Complete:
            end = min(end, self._current_row_count)
        with self._cache_lock:
            begin, end = self._cache.smallest_non_available_range(begin, end)
        if end != begin:
            self._worker.trigger_fetch(self._life, begin, end)

    def complete_cache(self, progress=None) -> bool:
        """Load all rows into the cache (needed for copy/export of a full selection)."""
        if self._static:
            return True
        self.wait_until_idle()
        i = 0
        while i < self.rowCount() + self.chunk_size // 2:
            if progress is not None and not progress(i, self.rowCount()):
                return False
            self.trigger_cache_load(i)
            self._worker.wait_until_idle()
            i += self.chunk_size
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        return True

    def is_cache_complete(self) -> bool:
        with self._cache_lock:
            return len(self._cache.rows) >= self.rowCount()

    # ------------------------------------------------------------------ query setup
    def reset(self) -> None:
        self._clear_cache()
        self.beginResetModel()
        self.last_error = ""
        self._s_query = ""
        self._query = Query()
        self._table = None
        self._headers = []
        self._data_types = []
        self._static = False
        self.endResetModel()

    def set_table_query(self, query: Query) -> None:
        """Browse a table or view (port of ``setQuery(const sqlb::Query&)``)."""
        self.reset()
        self._query = query
        self._table = self.db.get_table_by_name(query.table)
        if self._table is None:
            return
        if not self._table.is_view():
            self._query.rowid_columns = self._table.rowid_columns()
        else:
            self._query.is_view = True
            if not self._query.rowid_columns:
                self._query.rowid_columns = ["_rowid_"]
        self._data_types = [sqlb.Affinity.Integer]
        self._headers = [",".join(self._query.rowid_columns)]
        for f in self._table.fields:
            self._headers.append(f.name)
            self._data_types.append(f.affinity())
        self._query.column_names = list(self._headers)
        self._apply_display_formats({c.original_column: c.selector for c in self._query.selected_columns
                                     if c.selector != c.original_column})
        self._update_and_run_query()

    def set_sql_query(self, sql: str, column_names: Optional[list[str]] = None) -> None:
        """Show the result of an arbitrary statement (port of ``setQuery(const QString&)``)."""
        self.reset()
        self._s_query = remove_comments_from_query(sql.strip())
        names = column_names if column_names is not None else self._probe_columns(self._s_query)
        self.beginResetModel()
        self._headers = list(names)
        self._data_types = [sqlb.Affinity.Blob] * len(names)
        self.endResetModel()
        self._worker.set_query(self._s_query, "")
        self.trigger_cache_load(self.chunk_size // 2 - 1)
        self.layoutChanged.emit()

    def set_static_rows(self, columns: list[str], rows: list[tuple]) -> None:
        """Show already materialised rows (e.g. INSERT ... RETURNING)."""
        self.reset()
        self.beginResetModel()
        self._static = True
        self._headers = list(columns)
        self._data_types = [sqlb.Affinity.Blob] * len(columns)
        with self._cache_lock:
            for i, r in enumerate(rows):
                self._cache.set(i, list(r))
        self._current_row_count = self._real_row_count = len(rows)
        self._row_count_available = RowCount.Complete
        self.endResetModel()
        self.finishedRowCount.emit()

    def _probe_columns(self, sql: str) -> list[str]:
        with self.db.get(self.tr("retrieving list of columns")) as conn:
            if conn is None:
                return []
            try:
                cur = conn.execute(f"SELECT * FROM ({sql.rstrip(';')}) LIMIT 0") if sql.lstrip()[:6].upper() in ("SELECT", "VALUES") \
                    else None
                if cur is None or cur.description is None:
                    # PRAGMA/EXPLAIN/WITH: fall back to running the statement and reading its description
                    cur = conn.execute(sql)
                names = [d[0] for d in (cur.description or [])]
                cur.close()
                return names
            except Exception:
                try:
                    cur = conn.execute(sql)
                    names = [d[0] for d in (cur.description or [])]
                    cur.close()
                    return names
                except Exception as e:  # noqa: BLE001 - the error is shown to the user
                    self.last_error = str(e)
                    return []

    def _update_and_run_query(self) -> None:
        self._clear_cache()
        self._s_query = self._query.build_query(True)
        self._worker.set_query(self._s_query, self._query.build_count_query())
        self.trigger_cache_load(self.chunk_size // 2 - 1)
        self.layoutChanged.emit()

    def refresh(self) -> None:
        if self._static:
            return
        if not self._query.table.is_empty():
            # Schema might have changed; reload table definition
            self.set_table_query(self._query)
        elif self._s_query:
            self._clear_cache()
            self._worker.set_query(self._s_query, "")
            self.trigger_cache_load(self.chunk_size // 2 - 1)

    # ------------------------------------------------------------------ accessors
    @property
    def query(self) -> Query:
        return self._query

    @property
    def sql(self) -> str:
        return self._s_query

    @property
    def table(self) -> Optional[sqlb.Table]:
        return self._table

    def headers(self) -> list[str]:
        return list(self._headers)

    def column_name(self, column: int) -> str:
        return self._headers[column] if 0 <= column < len(self._headers) else ""

    def column_index(self, name: str) -> int:
        try:
            return self._headers.index(name)
        except ValueError:
            return -1

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self._current_row_count

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def filter_count(self) -> int:
        return len(self._query.where)

    def raw_value(self, row: int, column: int) -> Any:
        with self._cache_lock:
            r = self._cache.get(row)
        if r is None or column >= len(r):
            return None
        return r[column]

    def row_available(self, row: int) -> bool:
        with self._cache_lock:
            return row in self._cache

    def row_values(self, row: int) -> Optional[list]:
        with self._cache_lock:
            r = self._cache.get(row)
        return list(r) if r is not None else None

    # ------------------------------------------------------------------ header
    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Vertical:
            if role == Qt.DisplayRole:
                return str(section + 1)
            return None
        if section >= len(self._headers):
            return None
        name = self._headers[section]
        if role == Qt.EditRole:
            return name
        if role == Qt.FontRole:
            is_pk = name in self._query.rowid_columns or (self._table is not None and name in self._table.primary_key_names())
            is_fk = self.foreign_key_for(section) is not None
            f = QFont()
            f.setBold(is_pk)
            f.setItalic(is_fk)
            return f
        if role == Qt.ToolTipRole:
            if self._table is not None and section > 0:
                fld = self._table.find_field(name)
                if fld is not None:
                    parts = [f"<b>{name}</b>", fld.type or "(no type)"]
                    if name in self._table.primary_key_names():
                        parts.append("PRIMARY KEY")
                    if fld.notnull:
                        parts.append("NOT NULL")
                    if fld.unique:
                        parts.append("UNIQUE")
                    if fld.default_value:
                        parts.append("DEFAULT " + fld.default_value)
                    fk = self.foreign_key_for(section)
                    if fk is not None:
                        parts.append("→ " + fk.to_string())
                    return "<br>".join(parts)
            return name
        if role == Qt.DisplayRole:
            indicator = ""
            for i, ob in enumerate(self._query.order_by):
                if ob.expr == name:
                    indicator = " ▲" if ob.direction == SortDirection.Ascending else " ▼"
                    if len(self._query.order_by) > 1:
                        indicator += str(i + 1).translate(_SUPERSCRIPT)
                    break
            return name + indicator
        return None

    def foreign_key_for(self, column: int) -> Optional[sqlb.ForeignKeyClause]:
        if self._query.table.is_empty() or self._table is None or column <= 0:
            return None
        idx = column - 1
        if idx >= len(self._table.fields):
            return None
        return self._table.foreign_key([self._table.fields[idx].name])

    # ------------------------------------------------------------------ data
    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= self._current_row_count:
            return None
        row, col = index.row(), index.column()
        with self._cache_lock:
            r = self._cache.get(row)
        available = r is not None
        value = r[col] if available and col < len(r) else None
        if not available and role in (Qt.DisplayRole, Qt.EditRole, Qt.DecorationRole):
            # Load lazily when the view asks for a missing row
            self.trigger_cache_load(row)

        if role == Qt.DisplayRole:
            if not available:
                return self.tr("loading...")
            if value is None:
                return self.null_text
            if is_binary(value):
                return self.blob_text
            text = value_to_text(value)
            if len(text) > self.symbol_limit:
                return text[: self.symbol_limit] + " ..."
            return text
        if role == Qt.EditRole:
            if not available or value is None:
                return None
            return value_to_text(value)
        if role == RawValueRole:
            return value
        if role == Qt.FontRole:
            f = QFont(self.font)
            if not available or value is None or is_binary(value):
                f.setItalic(True)
            return f
        if role == Qt.ForegroundRole:
            if not available:
                return self.colors.loading_fg
            if value is None:
                return self.colors.null_fg
            if is_binary(value):
                return self.colors.bin_fg
            cf = self._matching_cond_format(col, value)
            if cf is not None and cf.fg:
                return QColor(cf.fg)
            if cf is not None and cf.bg:
                return readable_text_color(QColor(cf.bg))  # only a background was chosen: keep text legible
            if self.has_display_format(col) and self.colors.fmt_fg.isValid():
                return self.colors.fmt_fg
            return self.colors.reg_fg if self.colors.reg_fg.isValid() else None
        if role == Qt.BackgroundRole:
            if not available:
                return None
            if value is None:
                return self.colors.null_bg if self.colors.null_bg.isValid() else None
            if is_binary(value):
                return self.colors.bin_bg if self.colors.bin_bg.isValid() else None
            cf = self._matching_cond_format(col, value)
            if cf is not None and cf.bg:
                return QColor(cf.bg)
            if self.has_display_format(col) and self.colors.fmt_bg.isValid():
                return self.colors.fmt_bg
            return None
        if role == Qt.TextAlignmentRole:
            numeric = (col < len(self._data_types) and self._data_types[col] in (sqlb.Affinity.Integer, sqlb.Affinity.Float)) \
                or isinstance(value, (int, float))
            return int((Qt.AlignRight if numeric else Qt.AlignLeft) | Qt.AlignVCenter)
        if role == Qt.ToolTipRole:
            fk = self.foreign_key_for(col)
            if fk is not None:
                return self.tr("References %1(%2)\nHold Ctrl+Shift and click to jump there") \
                    .replace("%1", fk.table).replace("%2", ",".join(fk.columns))
            if isinstance(value, str) and len(value) > 80:
                return value[:2000]
            return None
        if role == Qt.DecorationRole:
            if self.image_preview and available and image_format(value):
                img = QImage()
                if img.loadFromData(bytes(value)):
                    return QPixmap.fromImage(img.scaledToHeight(64, Qt.SmoothTransformation))
            return None
        return None

    def is_editable(self, index: Optional[QModelIndex] = None) -> bool:
        if self._static or self._query.table.is_empty() or not self.db.is_open() or self.db.read_only:
            return False
        if (self._table is None or self._table.is_view()) and not self._query.has_custom_rowid_column():
            return False
        if index is not None and index.isValid() and self.has_display_format(index.column()):
            return False  # the cell shows a computed expression, not the stored value
        if index is not None and index.isValid() and self._table is not None:
            fld = self._table.find_field(self._headers[index.column()])
            if fld is not None and fld.generated:
                return False
        return True

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsEnabled
        fl = super().flags(index) | Qt.ItemIsDropEnabled
        if self.is_editable(index) and not is_binary(self.raw_value(index.row(), index.column())):
            fl |= Qt.ItemIsEditable
        return fl

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if not self.is_editable(index):
            return False
        return self.set_typed_data(index, False, value, role)

    def convert_for_column(self, column: int, value: Any, is_blob: bool = False) -> Any:
        """Port of the type decision in ``setTypedData``: BLOB if requested, INTEGER/REAL if the
        column affinity is numeric and the value parses, else TEXT."""
        if value is None:
            return None
        if is_blob:
            return value if isinstance(value, (bytes, bytearray)) else str(value).encode("utf-8")
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        text = value if isinstance(value, str) else value_to_text(value)
        aff = self._data_types[column] if column < len(self._data_types) else sqlb.Affinity.Blob
        if text == "" and self._table is not None:
            fld = self._table.find_field(self._headers[column])
            if fld is not None and fld.name in self._table.primary_key_names() and fld.is_integer():
                return 0
        if aff == sqlb.Affinity.Integer:
            try:
                return int(text.strip())
            except ValueError:
                pass
        if aff in (sqlb.Affinity.Float, sqlb.Affinity.Integer):
            try:
                if text.strip():
                    return float(text.strip())
            except ValueError:
                pass
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return text

    def set_typed_data(self, index: QModelIndex, is_blob: bool, value: Any, role: int = Qt.EditRole) -> bool:
        if self.reading_data() or not index.isValid() or role != Qt.EditRole:
            return False
        row, col = index.row(), index.column()
        with self._cache_lock:
            cached = self._cache.get(row)
        if cached is None:
            return False
        new_value = self.convert_for_column(col, value, is_blob)
        old_value = cached[col]
        if old_value == new_value and type(old_value) is type(new_value):
            return True
        self.db.set_undo_savepoint()
        if not self.db.update_record(self._query.table, self._headers[col], cached[0], new_value, self._query.rowid_columns):
            self.last_error = self.db.last_error
            self.errorOccurred.emit(self.tr("Error changing data:\n%1").replace("%1", self.db.last_error))
            return False
        with self._cache_lock:
            cached[col] = new_value
            if self._headers[col] in self._query.rowid_columns:
                if len(self._query.rowid_columns) == 1:
                    cached[0] = new_value
                else:
                    parts = []
                    for rc in self._query.rowid_columns:
                        v = cached[self._headers.index(rc, 1)]
                        t = value_to_text(v) if v is not None else ""
                        parts.append(f"{len(t.encode('utf-8'))}:{t}")
                    cached[0] = "".join(parts)
        self.dataChanged.emit(index.siblingAtColumn(0), index)
        return True

    # ------------------------------------------------------------------ rows
    def insertRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()) -> bool:
        if not self.is_editable() or self.reading_data():
            return False
        new_rows = []
        for _ in range(count):
            rowid = self.db.add_record(self._query.table)
            if rowid is None:
                self.last_error = self.db.last_error
                self.errorOccurred.emit(self.tr("Error adding record:\n%1").replace("%1", self.db.last_error))
                break
            data = self.db.get_row(self._query.table, rowid) or [None] * (len(self._headers) - 1)
            new_rows.append([rowid] + list(data))
        if not new_rows:
            return False
        self.beginInsertRows(parent, row, row + len(new_rows) - 1)
        with self._cache_lock:
            for i, r in enumerate(new_rows):
                self._cache.insert(row + i, r)
        self._current_row_count += len(new_rows)
        self._real_row_count += len(new_rows)
        self.endInsertRows()
        return True

    def removeRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()) -> bool:
        return self.remove_row_list(list(range(row, row + count)))

    def remove_row_list(self, rows: list[int]) -> bool:
        if not self.is_editable() or self.reading_data() or not rows:
            return False
        with self._cache_lock:
            rowids = [self._cache.get(r)[0] for r in rows if r in self._cache]
        self.db.set_undo_savepoint()
        if not self.db.delete_records(self._query.table, rowids, self._query.rowid_columns):
            self.last_error = self.db.last_error
            self.errorOccurred.emit(self.tr("Error deleting record:\n%1").replace("%1", self.db.last_error))
            return False
        for r in sorted(rows, reverse=True):
            self.beginRemoveRows(QModelIndex(), r, r)
            with self._cache_lock:
                self._cache.erase(r)
            self._current_row_count -= 1
            self._real_row_count -= 1
            self.endRemoveRows()
        return True

    def ditto_record(self, old_row: int) -> QModelIndex:
        """Duplicate a record (all non-PK values) at the end of the table."""
        if not self.is_editable() or self._table is None:
            return QModelIndex()
        if not self.insertRows(self.rowCount(), 1):
            return QModelIndex()
        new_row = self.rowCount() - 1
        pk = self._table.primary_key_names()
        first = 0
        for i, f in enumerate(self._table.fields):
            if f.generated or (pk and f.name in pk):
                continue
            col = i + 1
            first = first or col
            self.set_typed_data(self.index(new_row, col), isinstance(self.raw_value(old_row, col), bytes),
                                self.raw_value(old_row, col))
        return self.index(new_row, first or 1)

    # ------------------------------------------------------------------ display formats
    def display_formats(self) -> dict[str, str]:
        """Column name -> SQL expression for every column with a custom display format."""
        return {c.original_column: c.selector for c in self._query.selected_columns if c.selector != c.original_column}

    def display_format(self, column: int) -> str:
        return self.display_formats().get(self.column_name(column), "")

    def has_display_format(self, column: int) -> bool:
        return column > 0 and bool(self.display_format(column))

    def set_display_format(self, column: int, selector: str) -> None:
        """Show ``column`` through ``selector`` (port of ``TableBrowser::editDisplayFormat``);
        an empty selector or the bare column name restores the stored value."""
        name = self.column_name(column)
        if column <= 0 or not name or self._query.table.is_empty():
            return
        formats = self.display_formats()
        if selector and selector != sqlb.escape_identifier(name):
            formats[name] = selector
        else:
            formats.pop(name, None)
        if formats == self.display_formats():
            return
        self._apply_display_formats(formats)
        self._update_and_run_query()

    def _apply_display_formats(self, formats: dict[str, str]) -> None:
        # Like upstream, either every table column is listed (so the format slots in at the
        # right position) or none is and the query selects "*".
        if self._table is None or not any(self._table.find_field(n) is not None for n in formats):
            self._query.selected_columns = []
            return
        self._query.selected_columns = [SelectedColumn(f.name, formats.get(f.name, f.name)) for f in self._table.fields]

    # ------------------------------------------------------------------ conditional formats
    def cond_formats(self, column_name: str) -> list[CondFormat]:
        return list(self._cond_formats.get(column_name, []))

    def set_cond_formats(self, column_name: str, formats: list[CondFormat]) -> None:
        if formats:
            self._cond_formats[column_name] = list(formats)
        else:
            self._cond_formats.pop(column_name, None)
        self._emit_colors_changed()

    def add_cond_format(self, column_name: str, fmt: CondFormat) -> None:
        self.set_cond_formats(column_name, add_cond_format(self.cond_formats(column_name), fmt))

    def _emit_colors_changed(self) -> None:
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1),
                                  [Qt.ForegroundRole, Qt.BackgroundRole])

    def _matching_cond_format(self, column: int, value: Any) -> Optional[CondFormat]:
        formats = self._cond_formats.get(self._headers[column]) if column < len(self._headers) else None
        return self._matcher.first_match(formats, value) if formats else None

    # ------------------------------------------------------------------ sort / filter
    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        self.sort_by([OrderBy(self._headers[column], SortDirection.Ascending if order == Qt.AscendingOrder
                              else SortDirection.Descending)])

    def sort_by(self, columns: list[OrderBy]) -> None:
        if self._query.order_by == columns:
            return
        self._query.order_by = list(columns)
        if not self._query.table.is_empty():
            self._update_and_run_query()
        self.headerDataChanged.emit(Qt.Horizontal, 0, max(0, len(self._headers) - 1))

    def update_filter(self, column: int, value: str) -> None:
        name = self._headers[column]
        cond = filter_to_sql_condition(value, self.filter_escape)
        if cond:
            if self._query.where.get(name) == cond:
                return
            self._query.where[name] = cond
        else:
            if name not in self._query.where:
                return
            del self._query.where[name]
        if not self._query.table.is_empty():
            self._update_and_run_query()

    def update_global_filter(self, values: list[str]) -> None:
        conds = [c for c in (filter_to_sql_condition(v, self.filter_escape) for v in values) if c]
        if conds == self._query.global_where:
            return
        self._query.global_where = conds
        if not self._query.table.is_empty():
            self._update_and_run_query()

    def clear_filters(self) -> None:
        if not self._query.where and not self._query.global_where:
            return
        self._query.where.clear()
        self._query.global_where.clear()
        if not self._query.table.is_empty():
            self._update_and_run_query()
