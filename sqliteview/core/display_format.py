"""Per-column display formats. Port of the presets and checks in upstream
``ColumnDisplayFormatDialog``.

A display format is an SQL expression that replaces the column in the browse query
(``SELECT <format> AS "column"``); see ``Query.selected_columns``.
"""
from __future__ import annotations

import re
from typing import Optional

from .db import DBBrowserDB
from .sqlb import ObjectIdentifier, escape_identifier

# (key, label, template with {c} = escaped column name); None marks a separator
PRESETS: list[Optional[tuple[str, str, str]]] = [
    ("decimal", "Decimal number", "printf('%d', {c})"),
    ("exponent", "Exponent notation", "printf('%e', {c})"),
    ("hexblob", "Hex blob", "hex({c})"),
    ("hex", "Hex number", "printf('0x%x', {c})"),
    ("octal", "Octal number", "printf('%o', {c})"),
    ("round", "Round number", "round({c})"),
    None,
    ("appleDate", "Apple NSDate to date", "datetime('2001-01-01', {c} || ' seconds')"),
    ("javaEpoch", "Java epoch (milliseconds) to date",
     "strftime('%Y-%m-%d %H:%M:%S.', {c}/1000, 'unixepoch') || ({c}%1000)"),
    ("dotNetTicks", ".NET DateTime.Ticks to date", "datetime({c} / 10000000 - 62135596800, 'unixepoch')"),
    ("julian", "Julian day to date", "datetime({c})"),
    ("epoch", "Unix epoch to date", "datetime({c}, 'unixepoch')"),
    ("epochLocalTime", "Unix epoch to local time", "datetime({c}, 'unixepoch', 'localtime')"),
    ("webkitEpoch", "WebKit / Chromium epoch to date", "datetime({c} / 1e6 - 11644473600, 'unixepoch')"),
    ("webkitEpochLocalTime", "WebKit / Chromium epoch to local time",
     "datetime({c} / 1e6 - 11644473600, 'unixepoch', 'localtime')"),
    ("winDate", "Windows DATE to date", "datetime('1899-12-30', {c} || ' days')"),
    ("ddmmyyyyDate", "Date as dd/mm/yyyy", "strftime('%d/%m/%Y', {c})"),
    None,
    ("lower", "Lower case", "lower({c})"),
    ("upper", "Upper case", "upper({c})"),
    None,
    ("guid", "Binary GUID to text",
     "substr(hex({c}), 7, 2) || substr(hex({c}), 5, 2) || substr(hex({c}), 3, 2) || substr(hex({c}), 1, 2) || '-' || "
     "substr(hex({c}), 11, 2) || substr(hex({c}), 9, 2) || '-' || substr(hex({c}), 15, 2) || substr(hex({c}), 13, 2) || "
     "'-' || substr(hex({c}), 17, 4) || '-' || substr(hex({c}), 21, 12)"),
    ("json", "JSON", "json({c})"),
]


def preset_selector(key: str, column: str) -> str:
    """The SQL for preset ``key`` applied to ``column``; ``"default"`` is the bare column."""
    c = escape_identifier(column)
    if key == "default":
        return c
    for p in PRESETS:
        if p is not None and p[0] == key:
            return p[2].replace("{c}", c)
    raise KeyError(key)


def match_preset(selector: str, column: str) -> str:
    """Key of the preset that produced ``selector``: "default", a preset key, or "custom"."""
    if not selector or selector == escape_identifier(column):
        return "default"
    for p in PRESETS:
        if p is not None and preset_selector(p[0], column) == selector:
            return p[0]
    return "custom"


def validate_display_format(db: DBBrowserDB, table: ObjectIdentifier, column: str, selector: str) -> str:
    """Port of ``ColumnDisplayFormatDialog::accept``: returns an error message, or "" if usable."""
    c = escape_identifier(column)
    if selector != c and not re.search(r"[a-z]+[a-z_0-9]* *\(.*" + re.escape(c) + r".*\)", selector, re.I | re.S):
        return f"Custom display format must contain a function call applied to {c}"
    with db.get("checking display format", force_wait=True) as conn:
        if conn is None:
            return "No database is open."
        try:
            cur = conn.execute(f"SELECT {selector} FROM {table.to_string()} LIMIT 1")
            n = len(cur.description or [])
            cur.close()
        except Exception as e:  # noqa: BLE001 - the engine's message is what the user needs to see
            return f"Error in custom display format. Message from database engine:\n\n{e}"
    if n != 1:
        return f"Custom display format must return only one column but it returned {n}."
    return ""
