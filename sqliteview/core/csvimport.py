"""CSV import engine (port of upstream ``CSVParser`` + the insert path of ``ImportCsvDialog``).

Parsing uses Python's C-implemented ``csv`` module; the insert path uses ``executemany`` on a
single prepared statement inside a savepoint, the same strategy as the C++ code. Used by the
Import CSV dialog and the MCP server, so it never shows UI: problems are raised as exceptions.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, TextIO

from . import sqlb
from .db import DBBrowserDB

TYPE_DETECTION_ROWS = 20
INSERT_BATCH_ROWS = 5000
# Conflict clause keys -> SQL (the "On conflict" choice of the dialog)
CONFLICT_CLAUSES = {"abort": "", "ignore": "OR IGNORE", "replace": "OR REPLACE"}


@dataclass
class CsvOptions:
    separator: str = ","
    quote: str = '"'  # "" = no quoting
    encoding: str = "UTF-8"
    header: bool = True
    trim: bool = True
    sanitize_names: bool = False
    detect_types: bool = True


class CsvImportError(Exception):
    """Import failed; nothing of this file was kept."""


class ImportCancelled(Exception):
    """The progress callback asked to stop; nothing of this file was kept."""


class _CountingFile:
    """Wraps a text file to report how many characters have been consumed (for progress)."""

    def __init__(self, fh: TextIO):
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


def python_encoding(name: str) -> str:
    """Codec name for ``open()``; UTF-8 also strips a BOM."""
    enc = name.strip() or "UTF-8"
    return "utf-8-sig" if enc.upper() in ("UTF-8", "UTF8") else enc


def open_csv(path: str, opts: CsvOptions) -> TextIO:
    return open(path, encoding=python_encoding(opts.encoding), newline="", errors="replace")


def read_rows(fh, opts: CsvOptions) -> Iterator[list[str]]:
    """Parsed, non-empty rows (the header line included)."""
    kwargs: dict[str, Any] = dict(delimiter=opts.separator, doublequote=True, strict=False)
    if opts.quote:
        kwargs.update(quotechar=opts.quote, quoting=csv.QUOTE_MINIMAL)
    else:
        kwargs.update(quoting=csv.QUOTE_NONE, escapechar=None)
    for row in csv.reader(fh, **kwargs):
        if not row:
            continue
        yield [c.strip() for c in row] if opts.trim else row


def detect_fields(path: str, opts: CsvOptions) -> list[sqlb.Field]:
    """Port of ``generateFieldList``: names from the header, types from the first rows."""
    fields: list[sqlb.Field] = []
    with open_csv(path, opts) as fh:
        for row_num, row in enumerate(read_rows(fh, opts)):
            if row_num > TYPE_DETECTION_ROWS:
                break
            for i in range(len(fields), len(row)):
                name = ""
                if row_num == 0 and opts.header:
                    name = row[i]
                    if opts.sanitize_names:
                        name = re.sub(r"[^\w]", "_", name)
                if not name:
                    name = f"field{i + 1}"
                # make names unique
                base, n = name, 1
                while any(f.name.lower() == name.lower() for f in fields):
                    n += 1
                    name = f"{base}_{n}"
                fields.append(sqlb.Field(name, ""))
            if not opts.detect_types or (row_num == 0 and opts.header):
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
        if not f.type and opts.detect_types:
            f.type = "TEXT"
    return fields


def read_preview(path: str, opts: CsvOptions, limit: int = 100) -> list[list[str]]:
    """The first ``limit`` data rows (header skipped)."""
    rows: list[list[str]] = []
    with open_csv(path, opts) as fh:
        for i, row in enumerate(read_rows(fh, opts)):
            if i == 0 and opts.header:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def empty_values_for(db: DBBrowserDB, table: sqlb.Table, use_defaults: bool = True) -> list[Any]:
    """What an empty CSV field becomes in each column of an existing table: the column default
    (evaluated by SQLite), 0 / '' for NOT NULL columns, else NULL."""
    values: list[Any] = []
    for f in table.fields:
        if use_defaults and f.default_value:
            values.append(db.query_single_value(f"SELECT {f.default_value}", log=False))
        elif f.notnull:
            values.append(0 if f.is_integer() else "")
        else:
            values.append(None)
    return values


def import_csv_file(db: DBBrowserDB, path: str, table_name: str, opts: CsvOptions, fields: list[sqlb.Field],
                    to_existing: bool = False, conflict: str = "abort", empty_values: Optional[list[Any]] = None,
                    progress: Optional[Callable[[int], bool]] = None) -> int:
    """Import ``path`` into ``main.table_name`` inside its own savepoint and return the number of
    rows read. Creates the table from ``fields`` unless ``to_existing``. ``progress(permille)``
    returning False cancels. On any failure the savepoint is rolled back and
    :class:`CsvImportError` (or :class:`ImportCancelled`) is raised."""
    if conflict not in CONFLICT_CLAUSES:
        raise ValueError(f"unknown conflict mode {conflict!r}")
    ident = sqlb.ObjectIdentifier("main", table_name)
    savepoint = db.generate_savepoint_name("csvimport")
    if not db.set_savepoint(savepoint):
        raise CsvImportError(f"Creating restore point failed: {db.last_error}")
    if not to_existing and not db.create_table(ident, fields):
        error = db.last_error
        db.revert_to_savepoint(savepoint)
        raise CsvImportError(f"Creating the table failed: {error}")
    n = len(fields)
    sql = (f"INSERT {CONFLICT_CLAUSES[conflict]} INTO {sqlb.escape_identifier(table_name)} "
           f"VALUES({','.join('?' * n)})")
    total = max(1, os.path.getsize(path))
    rows_done = 0
    try:
        with open_csv(path, opts) as raw, db.get("importing CSV") as conn:
            fh = _CountingFile(raw)
            reader = read_rows(fh, opts)
            if opts.header:
                next(reader, None)
            batch: list[list[Any]] = []
            for row in reader:
                if len(row) < n:
                    row = row + [""] * (n - len(row))
                elif len(row) > n:
                    row = row[:n]
                if empty_values is not None:
                    batch.append([empty_values[i] if v == "" else v for i, v in enumerate(row)])
                else:
                    batch.append([None if v == "" else v for v in row])
                if len(batch) >= INSERT_BATCH_ROWS:
                    conn.executemany(sql, batch)
                    rows_done += len(batch)
                    batch.clear()
                    if progress is not None and not progress(int(fh.consumed * 1000 / total)):
                        raise ImportCancelled()
            if batch:
                conn.executemany(sql, batch)
                rows_done += len(batch)
    except ImportCancelled:
        db.revert_to_savepoint(savepoint)
        raise
    except (sqlite3.Error, csv.Error, OSError, UnicodeError) as e:
        db.revert_to_savepoint(savepoint)
        raise CsvImportError(f"Inserting row failed (around row {rows_done + 1}):\n{e}") from e
    db.log_sql(f"-- Imported {os.path.basename(path)} into {table_name}\n{sql}")
    return rows_done
