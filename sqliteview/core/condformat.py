"""Conditional formatting. Port of upstream ``CondFormat`` and the matching part of
``SqliteTableModel::getMatchingCondFormat``.

A rule pairs a filter (same syntax as the grid's filter row, see ``filters.py``) with a
foreground and/or background colour. Rules are checked in order; the first match wins and an
empty filter matches every non-NULL, non-binary cell.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Optional

from .data import value_to_text
from .db import _sql_regexp
from .filters import filter_to_sql_condition


@dataclass(frozen=True)
class CondFormat:
    filter: str
    fg: str = ""  # "#rrggbb", "" = keep the default colour
    bg: str = ""
    escape_character: str = "\\"

    @property
    def sql_condition(self) -> str:
        return filter_to_sql_condition(self.filter, self.escape_character)


def add_cond_format(formats: list[CondFormat], new: CondFormat) -> list[CondFormat]:
    """Port of ``addCondFormatToMap``: a rule with the same condition is replaced in place,
    a catch-all rule goes last, any other new rule goes first."""
    out = list(formats)
    for i, f in enumerate(out):
        if f.sql_condition == new.sql_condition:
            out[i] = new
            return out
    if not new.filter:
        out.append(new)
    else:
        out.insert(0, new)
    return out


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


class CondFormatMatcher:
    """Evaluates ``<value> <condition>`` on a private in-memory connection.

    Upstream asks the database itself, which would block on the row loader here; SQLite's
    comparison rules are identical on any connection, so a private one gives the same answers.
    Results are cached because the view asks for every visible cell on every repaint.
    """

    _CACHE_LIMIT = 20_000

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.create_function("REGEXP", 2, _sql_regexp, deterministic=True)
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, Any], bool] = {}

    def matches(self, fmt: CondFormat, value: Any) -> bool:
        if not fmt.filter:
            return True
        cond = fmt.sql_condition
        if not cond:
            return False
        bound = self._bind_value(value, cond)
        key = (cond, bound)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        with self._lock:
            try:
                row = self._conn.execute(f"SELECT ? {cond}", (bound,)).fetchone()
                result = bool(row and row[0] == 1)
            except sqlite3.Error:
                result = False  # e.g. an invalid regex: the rule simply doesn't apply
        if len(self._cache) >= self._CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = result
        return result

    def first_match(self, formats: list[CondFormat], value: Any) -> Optional[CondFormat]:
        for f in formats:
            if self.matches(f, value):
                return f
        return None

    @staticmethod
    def _bind_value(value: Any, condition: str) -> Any:
        # Upstream inlines numbers as literals unless the condition compares against a quoted
        # string; binding the number reproduces that without building SQL from cell data.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value if "'" not in condition else value_to_text(value)
        text = value_to_text(value)
        if "'" not in condition and _is_number(text.strip()):
            try:
                return int(text)
            except ValueError:
                return float(text)
        return text
