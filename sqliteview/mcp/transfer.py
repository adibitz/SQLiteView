"""Import and export tools. Files must be inside the server's folder sandbox; imports are staged
like every other change, exports write the file atomically."""
from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from typing import Any, Literal, Optional, Sequence

from ..core import sqlb
from ..core.csvimport import (CsvImportError, CsvOptions, detect_fields, empty_values_for, import_csv_file,
                              read_preview)
from ..core.rowexport import ROW_FORMATS, format_for_path, write_query_result
from ..core.sqlsplit import is_transaction_control, split_statements
from .editing import staged_result, execute_sql, insert_into
from .models import RowFilter
from .reading import filtered_query, reading_statement
from .session import DatabaseSession, ToolInputError, cell_value

IfExists = Literal["fail", "append", "replace"]
PREVIEW_ROWS = 5
MAX_JSON_IMPORT_BYTES = 200 * 1024 * 1024
MAX_SQL_IMPORT_BYTES = 200 * 1024 * 1024
# Dumps (sqlite3 .dump, iterdump) switch foreign keys off around their transaction; inside the import's
# savepoint that pragma would be a no-op anyway, and the AI connection may not change pragmas.
_DUMP_FK_PRAGMA_RE = re.compile(r"\s*PRAGMA\s+foreign_keys\s*=", re.I)


def _import_target(session: DatabaseSession, table: str, if_exists: IfExists) -> tuple[str, Optional[sqlb.Table]]:
    """("create" | "append" | "replace", the existing table when appending), checked against ``if_exists``."""
    if if_exists not in ("fail", "append", "replace"):
        raise ToolInputError("if_exists must be fail, append or replace")
    if not table:
        raise ToolInputError("The table name is empty.")
    existing = session.require_writable().get_table_by_name(sqlb.ObjectIdentifier("main", table))
    if existing is None:
        return "create", None
    if existing.is_view():
        raise ToolInputError(f"{existing.name!r} is a view; choose another table name.")
    if if_exists == "fail":
        raise ToolInputError(f"Table {existing.name!r} already exists. Use if_exists='append' to add the rows or "
                             "'replace' to drop and recreate it.")
    return ("append", existing) if if_exists == "append" else ("replace", None)


def _drop(db, name: str) -> None:
    if not db.execute_sql(f"DROP TABLE {sqlb.ObjectIdentifier('main', name).to_string()};"):
        raise ToolInputError(f"Couldn't drop {name!r}: {db.last_error}")


def _table_name(table: Optional[str], path: str) -> str:
    return (table or os.path.splitext(os.path.basename(path))[0]).strip()


# --------------------------------------------------------------------------- CSV

def import_csv(session: DatabaseSession, path: str, table: Optional[str] = None, separator: str = ",",
               quote: str = '"', encoding: str = "UTF-8", header: bool = True, if_exists: IfExists = "fail",
               on_conflict: str = "abort", dry_run: bool = False) -> dict[str, Any]:
    full = session.sandbox.resolve(path, must_exist=True)
    if len(separator) != 1 or len(quote) > 1:
        raise ToolInputError("separator must be one character and quote one character or empty.")
    name = _table_name(table, full)
    opts = CsvOptions(separator=separator, quote=quote, encoding=encoding, header=header)
    try:
        fields = detect_fields(full, opts)
        preview = read_preview(full, opts, PREVIEW_ROWS)
    except (OSError, csv.Error, LookupError, UnicodeError) as e:
        raise ToolInputError(f"Couldn't read {full}: {e}") from e
    if not fields:
        raise ToolInputError(f"{full} has no rows.")
    mode, existing = _import_target(session, name, if_exists)
    if existing is not None and len(existing.fields) != len(fields):
        raise ToolInputError(f"{existing.name!r} has {len(existing.fields)} columns but the file has {len(fields)}; "
                             "appending needs the same number of columns.")
    columns = [{"name": f.name, "type": f.type} for f in (existing.fields if existing else fields)]
    if dry_run:
        return {"dry_run": True, "file": full, "table": name, "action": mode, "columns": columns,
                "preview": preview}
    with session.staging(f"import {os.path.basename(full)} into {name} ({mode})") as db:
        if mode == "replace":
            _drop(db, name)
        empty = empty_values_for(db, existing) if existing is not None else None
        try:
            rows = import_csv_file(db, full, name, opts, fields, to_existing=existing is not None,
                                   conflict=on_conflict, empty_values=empty)
        except (CsvImportError, ValueError) as e:
            raise ToolInputError(str(e)) from e
    session.schema_changed()
    return staged_result(session, {"file": full, "table": name, "action": mode, "rows_read": rows, "columns": columns})


# --------------------------------------------------------------------------- JSON

def _json_type(values: list[Any]) -> str:
    kinds = {type(v) for v in values if v is not None}
    if kinds and kinds <= {int, bool}:
        return "INTEGER"
    if kinds and kinds <= {int, float, bool}:
        return "REAL"
    return "TEXT"


def _json_cell(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def import_json(session: DatabaseSession, path: str, table: Optional[str] = None, if_exists: IfExists = "fail",
                on_conflict: str = "abort", dry_run: bool = False) -> dict[str, Any]:
    """Import a JSON array of objects (one object per row; nested values are stored as JSON text)."""
    full = session.sandbox.resolve(path, must_exist=True)
    if os.path.getsize(full) > MAX_JSON_IMPORT_BYTES:
        raise ToolInputError(f"{full} is larger than {MAX_JSON_IMPORT_BYTES // 2**20} MB; convert it to CSV first.")
    try:
        with open(full, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise ToolInputError(f"Couldn't read {full}: {e}") from e
    if not isinstance(data, list) or not data or not all(isinstance(r, dict) and r for r in data):
        raise ToolInputError('The file must contain a non-empty JSON array of objects, e.g. [{"name": "a"}, …].')
    name = _table_name(table, full)
    keys: list[str] = []
    for r in data:
        keys += [k for k in r if k not in keys]
    rows = [{k: _json_cell(v) for k, v in r.items()} for r in data]
    mode, existing = _import_target(session, name, if_exists)
    columns = ([{"name": f.name, "type": f.type} for f in existing.fields] if existing is not None else
               [{"name": k, "type": _json_type([r.get(k) for r in rows])} for k in keys])
    if dry_run:
        return {"dry_run": True, "file": full, "table": name, "action": mode, "rows": len(rows), "columns": columns,
                "preview": [[cell_value(r.get(k)) for k in keys] for r in rows[:PREVIEW_ROWS]]}
    with session.staging(f"import {os.path.basename(full)} into {name} ({mode})") as db:
        ident = sqlb.ObjectIdentifier("main", name)
        if mode == "replace":
            _drop(db, name)
        if existing is None and not db.create_table(ident, [sqlb.Field(c["name"], c["type"]) for c in columns]):
            raise ToolInputError(f"Creating the table failed: {db.last_error}")
        # the rows are bound as parameters, so no authorizer is needed here
        with db.get("importing JSON") as conn:
            inserted = insert_into(conn, ident, db.get_table_by_name(ident), rows, on_conflict)
    session.schema_changed()
    return staged_result(session, {"file": full, "table": name, "action": mode, "inserted": inserted, "columns": columns})


# --------------------------------------------------------------------------- SQL

def import_sql(session: DatabaseSession, path: str, dry_run: bool = False) -> dict[str, Any]:
    """Run the statements of an .sql file as one change (the BEGIN/COMMIT and foreign-key pragma lines of
    dumps are skipped)."""
    full = session.sandbox.resolve(path, must_exist=True)
    if os.path.getsize(full) > MAX_SQL_IMPORT_BYTES:
        raise ToolInputError(f"{full} is larger than {MAX_SQL_IMPORT_BYTES // 2**20} MB.")
    try:
        with open(full, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        raise ToolInputError(f"Couldn't read {full}: {e}") from e
    body = [st for st in split_statements(text) if not is_transaction_control(st) and not _DUMP_FK_PRAGMA_RE.match(st)]
    result = execute_sql(session, "\n".join(body), dry_run, description=f"import {os.path.basename(full)}")
    result["file"] = full
    return result


# --------------------------------------------------------------------------- export

def export_data(session: DatabaseSession, path: str, table: Optional[str] = None, sql: Optional[str] = None,
                filters: Optional[Sequence[RowFilter]] = None, format: Optional[str] = None,
                header: bool = True, overwrite: bool = False) -> dict[str, Any]:
    if (table is None) == (sql is None):
        raise ToolInputError("Give either table (optionally with filters) or sql.")
    if sql is not None and filters:
        raise ToolInputError("filters only apply to a table export; put the conditions in the SQL instead.")
    fmt = format or format_for_path(path)
    if fmt not in ROW_FORMATS:
        raise ToolInputError(f"format must be one of {', '.join(ROW_FORMATS)}")
    full = session.export_target(path, overwrite)
    if table is not None:
        q, t = filtered_query(session, table, filters)
        query, name = q.build_query(False).strip(), t.name
    else:
        query, name = reading_statement(sql), "result"
    # The export runs on this connection with the read-only authorizer in place; no time limit, since
    # writing a whole large table can legitimately take longer than an interactive query.
    with session.reading(timeout=False):
        rows = write_query_result(session.db, query, full, fmt, header=header, table=name)
    return {"file": full, "format": fmt, "rows": rows, "bytes": os.path.getsize(full)}


def export_sql_dump(session: DatabaseSession, path: str, tables: Optional[Sequence[str]] = None,
                    schema: bool = True, data: bool = True, overwrite: bool = False) -> dict[str, Any]:
    """Write CREATE and INSERT statements (File › Export › Database to SQL file in the application)."""
    db = session.require_open()
    if not schema and not data:
        raise ToolInputError("Nothing to export: set schema and/or data.")
    if tables:
        names = [session.table(t)[1].name for t in tables]
    else:
        names = [n for n, t in db.schemata["main"].tables.items()
                 if not t.is_view() and not n.lower().startswith("sqlite_")]
    full = session.export_target(path, overwrite)
    # Dump next to the target and move it into place, so a failure never leaves a half-written file
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(full) + ".", suffix=".part", dir=os.path.dirname(full))
    os.close(fd)
    try:
        if not db.dump(tmp, names, insert_col_names=True, insert_new_syntax=False, keep_original=False,
                       export_schema=schema, export_data=data, keep_old_schema=False):
            raise ToolInputError(f"Export failed: {db.last_error}")
        os.replace(tmp, full)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"file": full, "tables": names, "bytes": os.path.getsize(full)}
