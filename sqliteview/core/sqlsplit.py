"""Split SQL scripts into statements.

Upstream walks scripts with ``sqlite3_prepare_v2``'s tail pointer, which the Python binding
doesn't expose. ``sqlite3.complete_statement`` is SQLite's own C tokenizer (it understands
strings, comments and ``CREATE TRIGGER ... BEGIN ... END``), so using it to find statement
boundaries yields the same split as the prepare/tail loop.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterator

_LEADING_RE = re.compile(r"(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?(?:\*/|\Z))*", re.S)
_ONLY_COMMENTS_RE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?(?:\*/|\Z)|;)*\Z", re.S)


@dataclass(frozen=True)
class Statement:
    start: int  # character offset of the first character (after leading whitespace)
    end: int  # character offset just past the terminating ';' (or end of text)
    text: str


def is_blank(sql: str) -> bool:
    """True if ``sql`` only contains whitespace, comments and semicolons."""
    return bool(_ONLY_COMMENTS_RE.match(sql))


def iter_statements(sql: str, start: int = 0, end: int | None = None) -> Iterator[Statement]:
    end = len(sql) if end is None else end
    pos = start
    while pos < end:
        # Skip leading whitespace and comments
        m = _LEADING_RE.match(sql, pos, end)
        pos = m.end()
        if pos >= end:
            return
        search = pos
        stmt_end = -1
        while True:
            semi = sql.find(";", search, end)
            if semi == -1:
                break
            if sqlite3.complete_statement(sql[pos:semi + 1]):
                stmt_end = semi + 1
                break
            search = semi + 1
        if stmt_end == -1:
            stmt_end = end
        text = sql[pos:stmt_end]
        if not is_blank(text):
            yield Statement(pos, stmt_end, text)
        pos = stmt_end


def split_statements(sql: str) -> list[str]:
    return [s.text for s in iter_statements(sql)]


def statement_at(sql: str, cursor: int) -> Statement | None:
    """Statement containing the character offset ``cursor`` (used by "Execute current")."""
    last = None
    for st in iter_statements(sql):
        if st.start <= cursor <= st.end:
            return st
        if st.start > cursor:
            return last or st
        last = st
    return last
