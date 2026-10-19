"""Schema tools: which tables exist, what their columns and keys are, and the full CREATE SQL."""
from __future__ import annotations

import glob
import os
from typing import Any

from ..core import sqlb
from .session import DATABASE_EXTENSIONS, DatabaseSession, ToolInputError, cell_value

MAX_LISTED_FILES = 200


def _user_objects(session: DatabaseSession) -> list[sqlb.Table]:
    objmap = session.require_open().schemata.get("main")
    if objmap is None:
        return []
    return [t for name, t in sorted(objmap.tables.items(), key=lambda kv: kv[0].lower())
            if not name.lower().startswith("sqlite_")]


def _kind(t: sqlb.Table) -> str:
    return "view" if t.is_view() else "virtual table" if t.is_virtual() else "table"


def _count(session: DatabaseSession, ident: sqlb.ObjectIdentifier) -> int:
    with session.reading() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {ident.to_string()}").fetchone()[0]


def database_info(session: DatabaseSession) -> dict[str, Any]:
    db = session.require_open()
    return {"database": session.path, "read_only": session.read_only or db.read_only,
            "uncommitted_changes": len(session.staged), "sqlite_version": db.sqlite_version()}


def list_database_files(session: DatabaseSession) -> dict[str, Any]:
    files: list[str] = []
    for root in session.sandbox.roots:
        for pattern in ("*", os.path.join("*", "*")):
            for p in glob.glob(os.path.join(glob.escape(root), pattern)):
                if p.lower().endswith(DATABASE_EXTENSIONS) and os.path.isfile(p):
                    try:
                        files.append(session.sandbox.resolve(p))  # skips links that lead outside
                    except ToolInputError:
                        pass
    files = sorted(set(files))
    return {"folders": session.sandbox.roots, "files": files[:MAX_LISTED_FILES],
            "truncated": len(files) > MAX_LISTED_FILES, "open": session.path or None}


def list_tables(session: DatabaseSession, include_row_counts: bool = True) -> dict[str, Any]:
    items = []
    for t in _user_objects(session):
        item: dict[str, Any] = {"name": t.name, "type": _kind(t), "columns": t.field_names()}
        if include_row_counts:
            item["rows"] = _count(session, sqlb.ObjectIdentifier("main", t.name))
        items.append(item)
    return {"database": session.path, "tables": items}


def describe_table(session: DatabaseSession, table: str, sample_rows: int = 3) -> dict[str, Any]:
    ident, t = session.table(table)
    db = session.db
    q = sqlb.escape_identifier(t.name)
    with db.exclusive("describing table") as conn:
        _, info = db.fetch_all(conn, f"PRAGMA main.table_xinfo({q});")
        _, fks = db.fetch_all(conn, f"PRAGMA main.foreign_key_list({q});")
    pk_order = {name: pos for name, pos in ((r[1], r[5]) for r in info) if pos}
    columns = []
    for _cid, name, type_, notnull, default, pk, hidden in info:
        f = t.find_field(name)
        col: dict[str, Any] = {"name": name, "type": type_ or "", "not_null": bool(notnull), "default": default,
                               "primary_key": bool(pk)}
        if f is not None and f.unique:
            col["unique"] = True
        if f is not None and f.collation:
            col["collation"] = f.collation
        if hidden in (2, 3):
            col["generated"] = f.generated.expression if f is not None and f.generated else True
        columns.append(col)
    foreign_keys: dict[int, dict[str, Any]] = {}
    for fk_id, _seq, ref_table, from_col, to_col, on_update, on_delete, _match in fks:
        entry = foreign_keys.setdefault(fk_id, {"columns": [], "references_table": ref_table,
                                                "references_columns": [], "on_update": on_update,
                                                "on_delete": on_delete})
        entry["columns"].append(from_col)
        entry["references_columns"].append(to_col)
    objmap = db.schemata["main"]
    indices = [{"name": i.name, "columns": [c.name for c in i.fields], "unique": i.unique,
                **({"where": i.where_expr} if i.where_expr else {})}
               for i in objmap.indices.values() if i.table.lower() == t.name.lower()]
    triggers = [trg.name for trg in objmap.triggers.values() if trg.table.lower() == t.name.lower()]
    result: dict[str, Any] = {
        "name": t.name, "type": _kind(t), "columns": columns,
        "primary_key": sorted(pk_order, key=pk_order.get), "foreign_keys": list(foreign_keys.values()),
        "indices": indices, "triggers": triggers, "rows": _count(session, ident), "sql": t.original_sql,
        "row_address": ("read-only (view)" if t.is_view() else
                        "_rowid_" if not t.without_rowid else f"primary key ({', '.join(t.rowid_columns())})"),
    }
    if sample_rows > 0:
        with session.reading() as conn:
            cur = conn.execute(f"SELECT * FROM {ident.to_string()} LIMIT ?", (min(sample_rows, 20),))
            result["sample_rows"] = [[cell_value(v) for v in r] for r in cur.fetchall()]
    return result


def schema_sql(session: DatabaseSession) -> str:
    """CREATE statements of every table, view, index and trigger, in a dependency-friendly order."""
    objmap = session.require_open().schemata.get("main")
    if objmap is None:
        return ""
    parts = [t.original_sql for t in _user_objects(session) if not t.is_view() and t.original_sql]
    parts += [t.original_sql for t in _user_objects(session) if t.is_view() and t.original_sql]
    parts += [o.original_sql for o in list(objmap.indices.values()) + list(objmap.triggers.values())
              if o.original_sql and not o.name.lower().startswith("sqlite_")]
    return "\n\n".join(p.rstrip().rstrip(";") + ";" for p in parts) + ("\n" if parts else "")
