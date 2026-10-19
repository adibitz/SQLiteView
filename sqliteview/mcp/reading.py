"""Read tools: run a SELECT, explain it, and filter/sort/page a table without writing SQL.

Filters go through the same filter syntax as the application's filter row
(``core/filters.py``) and the same SELECT builder (``core/query.py``), so a result can be
reproduced in SQliteView.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional, Sequence, Union

from ..core import sqlb
from ..core.filters import FILTER_OPERATORS, build_filter_text, filter_to_sql_condition, tokenize_global_filter
from ..core.query import OrderBy, Query, SelectedColumn, SortDirection
from ..core.runsql import returns_rows, statement_type
from ..core.settings import Settings
from ..core.sqlsplit import split_statements
from .models import RowFilter, SortKey
from .session import DEFAULT_ROW_LIMIT, DatabaseSession, ToolInputError, cell_value, clamp_limit, param_value

Params = Union[list[Any], dict[str, Any], None]
_VALUE_COUNT = {key: n for key, _label, n in FILTER_OPERATORS}
# Leading characters the filter syntax would read as an operator instead of text
_SYNTAX_CHARS = ("=", "<", ">", "/")


def single_statement(sql: str) -> str:
    statements = [s for s in split_statements(sql) if s.strip().rstrip(";").strip()]
    if len(statements) != 1:
        raise ToolInputError(f"Expected exactly one SQL statement, got {len(statements)}.")
    return statements[0]


def reading_statement(sql: str) -> str:
    """``sql`` if it is one statement that only reads; raises otherwise."""
    stmt = single_statement(sql)
    if not returns_rows(stmt, statement_type(stmt)):
        raise ToolInputError("This tool only runs reading statements (SELECT, WITH … SELECT, VALUES, EXPLAIN, "
                             "PRAGMA without a value). Use execute_sql to change data or schema.")
    return stmt


def _params(params: Params) -> Union[list[Any], dict[str, Any]]:
    if params is None:
        return []
    if isinstance(params, dict):
        return {k: param_value(v) for k, v in params.items()}
    return [param_value(v) for v in params]


def _page(cur: sqlite3.Cursor, limit: int, offset: int, skip: bool = True) -> dict[str, Any]:
    """Rows ``offset`` … ``offset + limit`` of ``cur`` (``skip=False`` when the SQL already has the OFFSET)."""
    names = [d[0] for d in cur.description or []]
    for _ in range(offset if skip else 0):
        if cur.fetchone() is None:
            break
    rows = cur.fetchmany(limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    out: dict[str, Any] = {"columns": names, "rows": [[cell_value(v) for v in r] for r in rows],
                           "returned": len(rows), "truncated": truncated}
    if truncated:
        out["next_offset"] = offset + limit
    return out


def run_query(session: DatabaseSession, sql: str, params: Params = None, limit: int = DEFAULT_ROW_LIMIT,
              offset: int = 0) -> dict[str, Any]:
    stmt = reading_statement(sql)
    limit = clamp_limit(limit)
    if offset < 0:
        raise ToolInputError("offset can't be negative")
    started = time.monotonic()
    with session.reading() as conn:
        result = _page(conn.execute(stmt, _params(params)), limit, offset)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def explain_query(session: DatabaseSession, sql: str) -> dict[str, Any]:
    """Checks one statement without running it and shows how SQLite would execute it."""
    stmt = single_statement(sql)
    try:
        with session.explaining() as conn:
            plan = conn.execute("EXPLAIN QUERY PLAN " + stmt).fetchall()
    except sqlite3.Error as e:
        return {"valid": False, "error": str(e)}
    return {"valid": True, "statement_type": statement_type(stmt).name.lower(),
            "reads_only": returns_rows(stmt, statement_type(stmt)),
            "plan": [{"id": r[0], "parent": r[1], "detail": r[3]} for r in plan]}


# --------------------------------------------------------------------------- filtering

def _filter_condition(f: RowFilter) -> str:
    value = "" if f.value is None else str(f.value)
    value2 = "" if f.value2 is None else str(f.value2)
    needed = _VALUE_COUNT[f.op]
    if needed >= 1 and value == "":
        raise ToolInputError(f"The {f.op} filter on {f.column!r} needs a value.")
    if f.op == "between":
        try:
            low, high = float(value), float(value2)
        except ValueError:
            raise ToolInputError("between needs two numbers (value and value2); compare text or dates with "
                                 "ge and le instead.") from None
        if low >= high:
            raise ToolInputError("between needs value < value2.")
    if f.op in ("contains", "starts", "ends") and (value.startswith(_SYNTAX_CHARS) or "~" in value):
        raise ToolInputError(f"Text searches can't start with = < > / or contain ~ (got {value!r}); use run_query "
                             "with LIKE for this pattern.")
    cond = filter_to_sql_condition(build_filter_text(f.op, value, value2), Settings.get("databrowser", "filter_escape"))
    if not cond:
        raise ToolInputError(f"The filter {f.op} {value!r} on {f.column!r} matches everything; leave it out.")
    return cond


def filtered_query(session: DatabaseSession, table: str, filters: Optional[Sequence[RowFilter]] = None,
                   search: str = "", sort: Optional[Sequence[SortKey]] = None,
                   columns: Optional[Sequence[str]] = None) -> tuple[Query, sqlb.Table]:
    """A :class:`Query` (the table browser's SELECT builder) for the given conditions."""
    ident, t = session.table(table)
    q = Query(table=ident, is_view=t.is_view(), column_names=t.field_names(), rowid_columns=t.rowid_columns())
    for f in filters or ():
        col = session.column(t, f.column)
        cond = _filter_condition(f)
        # Query keeps one condition per column; further ones on the same column are ANDed onto it
        q.where[col] = f"{q.where[col]} AND {sqlb.escape_identifier(col)} {cond}" if col in q.where else cond
    escape = Settings.get("databrowser", "filter_escape")
    for term in tokenize_global_filter(search or ""):
        cond = filter_to_sql_condition(term, escape)
        if cond:
            q.global_where.append(cond)
    for s in sort or ():
        direction = SortDirection.Descending if s.descending else SortDirection.Ascending
        q.order_by.append(OrderBy(session.column(t, s.column), direction))
    if columns:
        q.selected_columns = [SelectedColumn(c, c) for c in (session.column(t, name) for name in columns)]
    return q, t


def filter_rows(session: DatabaseSession, table: str, filters: Optional[Sequence[RowFilter]] = None,
                search: str = "", sort: Optional[Sequence[SortKey]] = None, columns: Optional[Sequence[str]] = None,
                limit: int = DEFAULT_ROW_LIMIT, offset: int = 0, include_rowid: bool = True) -> dict[str, Any]:
    limit = clamp_limit(limit)
    if offset < 0:
        raise ToolInputError("offset can't be negative")
    q, t = filtered_query(session, table, filters, search, sort, columns)
    with_rowid = include_rowid and not t.is_view()
    sql = q.build_query(with_rowid).strip() + f" LIMIT {limit + 1} OFFSET {offset}"
    with session.reading() as conn:
        total = conn.execute(q.build_count_query()).fetchone()[0]
        result = _page(conn.execute(sql), limit, offset, skip=False)
    if with_rowid:
        result["columns"][0] = "_rowid_"
    result.update(table=t.name, total_matching=total, sql=q.build_query(False).strip())
    return result
