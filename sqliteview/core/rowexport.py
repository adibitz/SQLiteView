"""Turn rows into CSV / TSV / JSON / SQL INSERT / Markdown text (export and copy of selections)."""
from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence

from . import sqlb
from .data import is_binary, value_to_text

if TYPE_CHECKING:
    from .db import DBBrowserDB

ROW_FORMATS = {
    "csv": "CSV",
    "tsv": "Tab separated",
    "json": "JSON",
    "sql": "SQL INSERT statements",
    "md": "Markdown table",
}


def csv_field(v: Any, sep: str, quote: str, newline: str) -> str:
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


def json_value(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode("ascii")
    return v


class ExportCancelled(Exception):
    """The progress callback asked to stop; no file was written."""


def format_rows(fmt: str, names: Sequence[str], rows: Iterable[Sequence[Any]], table: str = "table",
                sep: str = ",", quote: str = '"', newline: str = "\r\n", header: bool = True,
                pretty_json: bool = True) -> str:
    """Render ``rows`` (sequences of SQLite values, in ``names`` order) in one of ``ROW_FORMATS``."""
    rows = list(rows)
    if fmt == "json":
        data = [{n: json_value(v) for n, v in zip(names, r)} for r in rows]
        return json.dumps(data, ensure_ascii=False, indent=4 if pretty_json else None)
    if fmt == "sql":
        from .db import sql_literal
        cols = ",".join(sqlb.escape_identifiers(names))
        target = sqlb.escape_identifier(table)
        return "\n".join(f"INSERT INTO {target} ({cols}) VALUES ({','.join(sql_literal(v) for v in r)});" for r in rows) + "\n"
    if fmt == "md":
        def cell(v: Any) -> str:
            if v is None:
                return "NULL"
            if isinstance(v, (bytes, bytearray)) and is_binary(v):
                return f"BLOB ({len(v)} bytes)"
            return value_to_text(v).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        lines = ["| " + " | ".join(n.replace("|", "\\|") for n in names) + " |",
                 "|" + "|".join(" --- " for _ in names) + "|"]
        lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
        return "\n".join(lines) + "\n"
    if fmt == "tsv":
        sep, quote = "\t", '"'
    out = []
    if header:
        q = quote or ""
        out.append(sep.join(f"{q}{n.replace(q, q + q) if q else n}{q}" for n in names))
    out += [sep.join(csv_field(v, sep, quote, newline) for v in r) for r in rows]
    return newline.join(out) + newline


def format_for_path(path: str) -> str:
    """Pick a row format from a file extension (defaults to CSV)."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"json": "json", "sql": "sql", "md": "md", "markdown": "md", "tsv": "tsv", "tab": "tsv"}.get(ext, "csv")


def write_query_result(db: "DBBrowserDB", query: str, path: str, fmt: str = "csv", *, header: bool = True,
                       sep: str = ",", quote: str = '"', newline: str = "\r\n", pretty_json: bool = True,
                       table: str = "table", progress: Optional[Callable[[int], bool]] = None) -> int:
    """Run ``query`` and write its rows to ``path`` in one of ``ROW_FORMATS``; returns the row count.

    The file appears atomically (written to ``path + '.part'`` first). CSV/TSV stream row by row;
    the other formats are rendered in one go. ``progress(rows_so_far)`` returning False cancels
    with :class:`ExportCancelled`; SQLite and file errors propagate. Nothing is left behind on failure.
    """
    from .db import _text_factory_fallback

    if fmt not in ROW_FORMATS:
        raise ValueError(f"unknown export format {fmt!r}")
    if fmt == "tsv":
        sep, quote = "\t", '"'
    tmp = path + ".part"
    n = 0
    with db.get("exporting data") as conn:
        if conn is None:
            raise OSError("No database file opened")
        conn.text_factory = _text_factory_fallback
        try:
            cur = db.run(conn, query)
            names = [d[0] for d in cur.description or []]
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                if fmt in ("csv", "tsv"):
                    if header:
                        q = quote or ""
                        fh.write(sep.join(f"{q}{c.replace(q, q + q) if q else c}{q}" for c in names) + newline)
                    for r in cur:
                        fh.write(sep.join(csv_field(v, sep, quote, newline) for v in r) + newline)
                        n += 1
                        if progress is not None and n % 5000 == 0 and not progress(n):
                            raise ExportCancelled()
                else:
                    rows = []
                    for r in cur:
                        rows.append(r)
                        n += 1
                        if progress is not None and n % 2000 == 0 and not progress(n):
                            raise ExportCancelled()
                    fh.write(format_rows(fmt, names, rows, table=table, pretty_json=pretty_json))
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        finally:
            conn.text_factory = str
    os.replace(tmp, path)
    return n
