"""Edit tools: insert, update and delete rows, create tables, run SQL scripts, and commit or
revert. Every change is staged in a savepoint until ``commit_changes``."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional, Sequence

from ..core import sqlb
from ..core.csvimport import CONFLICT_CLAUSES
from ..core.db import DBBrowserDB
from ..core.runsql import StatementType, statement_type
from ..core.sqlsplit import split_statements
from .models import ColumnSpec, RowFilter
from .reading import filtered_query
from .session import (DEFAULT_ROW_LIMIT, DatabaseSession, ToolInputError, cell_value, describe_sqlite_error,
                      param_value)

MAX_ROWS_PER_CALL = 5000
SAMPLE_ROWS = 5
STAGED_NOTE = "Staged, not saved yet: call commit_changes to write it to the file, or revert_changes to undo."
_REFERENCES_RE = re.compile(r"^\s*([^()]+?)\s*(?:\(\s*([^()]*?)\s*\))?\s*$")


def _conflict(on_conflict: str) -> str:
    if on_conflict not in CONFLICT_CLAUSES:
        raise ToolInputError(f"on_conflict must be one of {', '.join(CONFLICT_CLAUSES)}")
    return CONFLICT_CLAUSES[on_conflict]


def staged_result(session: DatabaseSession, result: dict[str, Any]) -> dict[str, Any]:
    result.update(uncommitted_changes=len(session.staged), note=STAGED_NOTE)
    return result


# --------------------------------------------------------------------------- insert

def insert_into(conn: sqlite3.Connection, ident: sqlb.ObjectIdentifier, t: sqlb.Table, rows: Sequence[dict[str, Any]],
                on_conflict: str = "abort") -> int:
    """Insert ``rows`` (column -> value) keeping their order; returns how many were inserted (rows written by
    triggers don't count)."""
    clause = _conflict(on_conflict)
    run_cols: Optional[tuple[str, ...]] = None
    batch: list[list[Any]] = []
    inserted = 0

    def flush() -> None:
        nonlocal inserted
        if run_cols is None or not batch:
            return
        cols = ",".join(sqlb.escape_identifiers(run_cols))
        cur = conn.executemany(f"INSERT {clause} INTO {ident.to_string()} ({cols}) "
                               f"VALUES ({','.join('?' * len(run_cols))})", batch)
        inserted += cur.rowcount
        batch.clear()

    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not row:
            raise ToolInputError(f"Row {i} must be an object of column names to values.")
        cols = tuple(DatabaseSession.column(t, name) for name in row)
        if cols != run_cols:  # consecutive rows with the same columns share one prepared statement
            flush()
            run_cols = cols
        batch.append([param_value(v) for v in row.values()])
    flush()
    return inserted


def insert_rows(session: DatabaseSession, table: str, rows: Sequence[dict[str, Any]],
                on_conflict: str = "abort") -> dict[str, Any]:
    ident, t = session.table(table, writable=True)
    if not rows:
        raise ToolInputError("rows is empty.")
    if len(rows) > MAX_ROWS_PER_CALL:
        raise ToolInputError(f"At most {MAX_ROWS_PER_CALL} rows per call; split the insert or use import_csv.")
    with session.writing(f"insert {len(rows)} row(s) into {t.name}") as conn:
        inserted = insert_into(conn, ident, t, rows, on_conflict)
        last = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return staged_result(session, {"table": t.name, "inserted": inserted, "skipped": len(rows) - inserted,
                             "last_rowid": last})


# --------------------------------------------------------------------------- update / delete

def _target(session: DatabaseSession, table: str, filters: Optional[Sequence[RowFilter]],
            rowids: Optional[Sequence[Any]], all_rows: bool) -> tuple[sqlb.ObjectIdentifier, sqlb.Table, str, list[Any]]:
    """(table, model, WHERE clause, parameters) for exactly one of filters / rowids / all_rows."""
    ident, t = session.table(table, writable=True)
    if sum((bool(filters), rowids is not None, all_rows)) != 1:
        raise ToolInputError("Give exactly one of: filters, rowids, or all_rows=true (only when the user means "
                             "every row).")
    if filters:
        q, _ = filtered_query(session, table, filters)
        return ident, t, q.build_where_part(), []
    if rowids is not None:
        if not rowids or len(rowids) > MAX_ROWS_PER_CALL:
            raise ToolInputError(f"rowids needs 1 to {MAX_ROWS_PER_CALL} values (the _rowid_ column of filter_rows).")
        selector = DBBrowserDB.rowid_selector(t.rowid_columns())
        return ident, t, f"WHERE {selector} IN ({','.join('?' * len(rowids))})", [param_value(r) for r in rowids]
    return ident, t, "", []


def _preview(session: DatabaseSession, ident: sqlb.ObjectIdentifier, t: sqlb.Table, where: str,
             params: list[Any]) -> tuple[int, dict[str, Any]]:
    selector = DBBrowserDB.rowid_selector(t.rowid_columns())
    with session.reading() as conn:
        matched = conn.execute(f"SELECT COUNT(*) FROM {ident.to_string()} {where}", params).fetchone()[0]
        cur = conn.execute(f"SELECT {selector} AS _rowid_, * FROM {ident.to_string()} {where} LIMIT {SAMPLE_ROWS}",
                           params)
        sample = {"columns": [d[0] for d in cur.description],
                  "rows": [[cell_value(v) for v in r] for r in cur.fetchall()]}
    return matched, sample


def update_rows(session: DatabaseSession, table: str, values: dict[str, Any],
                filters: Optional[Sequence[RowFilter]] = None, rowids: Optional[Sequence[Any]] = None,
                all_rows: bool = False, dry_run: bool = False) -> dict[str, Any]:
    if not values:
        raise ToolInputError("values is empty: give column -> new value.")
    ident, t, where, params = _target(session, table, filters, rowids, all_rows)
    cols = [session.column(t, c) for c in values]
    sql = (f"UPDATE {ident.to_string()} SET {','.join(f'{sqlb.escape_identifier(c)}=?' for c in cols)} "
           f"{where}").strip()
    matched, sample = _preview(session, ident, t, where, params)
    if dry_run:
        return {"dry_run": True, "table": t.name, "would_update": matched, "sample_before": sample, "sql": sql}
    with session.writing(f"update {matched} row(s) in {t.name}") as conn:
        updated = conn.execute(sql, [param_value(v) for v in values.values()] + params).rowcount
    return staged_result(session, {"table": t.name, "updated": updated})


def delete_rows(session: DatabaseSession, table: str, filters: Optional[Sequence[RowFilter]] = None,
                rowids: Optional[Sequence[Any]] = None, all_rows: bool = False, dry_run: bool = False) -> dict[str, Any]:
    ident, t, where, params = _target(session, table, filters, rowids, all_rows)
    sql = f"DELETE FROM {ident.to_string()} {where}".strip()
    matched, sample = _preview(session, ident, t, where, params)
    if dry_run:
        return {"dry_run": True, "table": t.name, "would_delete": matched, "sample": sample, "sql": sql}
    with session.writing(f"delete {matched} row(s) from {t.name}") as conn:
        deleted = conn.execute(sql, params).rowcount  # rows removed by ON DELETE CASCADE don't count
    return staged_result(session, {"table": t.name, "deleted": deleted})


# --------------------------------------------------------------------------- schema

def table_model(name: str, columns: Sequence[ColumnSpec], without_rowid: bool = False) -> sqlb.Table:
    """The schema object model of a new table (rendered to SQL by :meth:`sqlb.Table.sql`)."""
    if not name.strip():
        raise ToolInputError("The table name is empty.")
    if not columns:
        raise ToolInputError("A table needs at least one column.")
    t = sqlb.Table(name.strip())
    for c in columns:
        if t.find_field(c.name) is not None:
            raise ToolInputError(f"Column {c.name!r} appears twice.")
        t.fields.append(sqlb.Field.make(c.name, c.type, notnull=c.not_null, default=c.default or "",
                                        check=c.check or "", unique=c.unique))
        if c.references:
            m = _REFERENCES_RE.match(c.references)
            if not m:
                raise ToolInputError(f"references must look like table or table(column), got {c.references!r}")
            ref_cols = [x.strip() for x in m.group(2).split(",")] if m.group(2) else []
            t.add_foreign_key([c.name], sqlb.ForeignKeyClause(m.group(1), ref_cols))
    pk = [c for c in columns if c.primary_key]
    autoincrement = any(c.autoincrement for c in columns)
    if autoincrement and not (len(pk) == 1 and pk[0].autoincrement and pk[0].type.upper() == "INTEGER"):
        raise ToolInputError("autoincrement only works on a single INTEGER primary key column.")
    if pk:
        t.set_primary_key_columns([c.name for c in pk], auto_increment=autoincrement)
    if without_rowid:
        if not pk or autoincrement:
            raise ToolInputError("without_rowid needs a primary key and no autoincrement.")
        t.without_rowid = True
    return t


def create_table(session: DatabaseSession, name: str, columns: Sequence[ColumnSpec], if_not_exists: bool = False,
                 without_rowid: bool = False) -> dict[str, Any]:
    db = session.require_writable()
    t = table_model(name, columns, without_rowid)
    if not if_not_exists and db.get_table_by_name(sqlb.ObjectIdentifier("main", t.name)) is not None:
        raise ToolInputError(f"A table or view named {t.name!r} already exists.")
    sql = t.sql("main", if_not_exists)
    with session.writing(f"create table {t.name}") as conn:
        conn.execute(sql)
    session.schema_changed()
    return staged_result(session, {"created": t.name, "sql": sql})


# --------------------------------------------------------------------------- scripts

_NOT_IN_SCRIPTS = {StatementType.Vacuum: "VACUUM", StatementType.Attach: "ATTACH", StatementType.Detach: "DETACH"}


def execute_sql(session: DatabaseSession, sql: str, dry_run: bool = False,
                description: str = "") -> dict[str, Any]:
    """Run one or more statements as a single change; any error rolls the whole script back."""
    session.require_writable()
    statements = [s for s in split_statements(sql) if s.strip().rstrip(";").strip()]
    if not statements:
        raise ToolInputError("No SQL statement given.")
    for st in statements:
        kind = statement_type(st)
        if kind in _NOT_IN_SCRIPTS:
            raise ToolInputError(f"{_NOT_IN_SCRIPTS[kind]} isn't available through the AI connection; use SQliteView.")
    ddl = any(statement_type(st) in (StatementType.Create, StatementType.Alter, StatementType.Drop)
              for st in statements)
    returned: Optional[dict[str, Any]] = None
    label = description or f"execute {len(statements)} statement(s)"
    with session.writing(label, dry_run) as conn:
        before = conn.total_changes
        for i, st in enumerate(statements, 1):
            try:
                cur = conn.execute(st)
                if cur.description:  # e.g. INSERT … RETURNING
                    rows = cur.fetchmany(DEFAULT_ROW_LIMIT)
                    returned = {"columns": [d[0] for d in cur.description],
                                "rows": [[cell_value(v) for v in r] for r in rows]}
                    cur.fetchall()
            except sqlite3.Error as e:
                raise ToolInputError(f"Statement {i} of {len(statements)} failed, so nothing was changed. "
                                     f"{describe_sqlite_error(e, session.query_timeout)}\nStatement: {st[:300]}") from e
        changed = conn.total_changes - before
    if ddl and not dry_run:
        session.schema_changed()
    result: dict[str, Any] = {"statements": len(statements), "rows_changed": changed, "schema_changed": ddl}
    if returned is not None:
        result["returned"] = returned
    if dry_run:
        result.update(dry_run=True, note="Rolled back: nothing was changed.")
        return result
    return staged_result(session, result)


# --------------------------------------------------------------------------- transaction

def pending_changes(session: DatabaseSession) -> dict[str, Any]:
    db = session.require_open()
    return {"database": session.path, "uncommitted": bool(db.dirty), "changes": list(session.staged)}


def commit_changes(session: DatabaseSession) -> dict[str, Any]:
    done = session.commit()
    return {"database": session.path, "committed": done,
            "note": "Saved to the file." if done else "There was nothing to save."}


def revert_changes(session: DatabaseSession) -> dict[str, Any]:
    undone = session.revert()
    return {"database": session.path, "reverted": undone,
            "note": "All uncommitted changes were undone." if undone else "There was nothing to undo."}
