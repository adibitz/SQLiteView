"""SELECT query builder for table browsing. Port of upstream ``src/sql/Query.{h,cpp}``."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .sqlb import ObjectIdentifier, escape_identifier


class SortDirection(IntEnum):
    Ascending = 0
    Descending = 1


@dataclass
class OrderBy:
    expr: str
    direction: SortDirection = SortDirection.Ascending
    is_expression: bool = False

    def to_sql(self) -> str:
        d = " ASC" if self.direction == SortDirection.Ascending else " DESC"
        return (self.expr if self.is_expression else escape_identifier(self.expr)) + d


@dataclass
class SelectedColumn:
    original_column: str
    selector: str


@dataclass
class Query:
    table: ObjectIdentifier = field(default_factory=ObjectIdentifier)
    is_view: bool = False
    column_names: list[str] = field(default_factory=list)
    rowid_columns: list[str] = field(default_factory=lambda: ["_rowid_"])
    selected_columns: list[SelectedColumn] = field(default_factory=list)
    where: dict[str, str] = field(default_factory=dict)  # column -> condition (e.g. "LIKE '%a%'")
    global_where: list[str] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)

    def clear(self) -> None:
        self.table = ObjectIdentifier()
        self.rowid_columns = ["_rowid_"]
        self.selected_columns.clear()
        self.where.clear()
        self.order_by.clear()

    def has_custom_rowid_column(self) -> bool:
        return len(self.rowid_columns) != 1 or self.rowid_columns[0] not in ("rowid", "_rowid_")

    def _selector_for(self, name: str) -> str:
        for c in self.selected_columns:
            if c.original_column == name and c.selector != name:
                return c.selector
        return escape_identifier(name)

    def build_where_part(self) -> str:
        if not self.where and not self.global_where:
            return ""
        parts = [f"{self._selector_for(col)} {cond}" for col, cond in self.where.items()]
        for w in self.global_where:
            ors = " OR ".join(f"{self._selector_for(c)} {w}" for c in self.column_names)
            parts.append(f"({ors})")
        return "WHERE " + " AND ".join(parts)

    def build_query(self, with_rowid: bool) -> str:
        selector = ""
        if with_rowid:
            if len(self.rowid_columns) == 1:
                if self.is_view and not self.has_custom_rowid_column():
                    selector = "NULL,"
                else:
                    selector = escape_identifier(self.rowid_columns[0]) + ","
            else:
                selector = ("sqlb_make_single_value(" + ",".join(escape_identifier(c) for c in self.rowid_columns)
                            + "),")
        if not self.selected_columns:
            selector += "*"
        else:
            cols = []
            for c in self.selected_columns:
                if c.original_column != c.selector:
                    cols.append(f"{c.selector} AS {escape_identifier(c.original_column)}")
                else:
                    cols.append(escape_identifier(c.original_column))
            selector += ",".join(cols)
        order_by = ",".join(o.to_sql() for o in self.order_by)
        if order_by:
            order_by = "ORDER BY " + order_by
        return f"SELECT {selector} FROM {self.table.to_string()} {self.build_where_part()} {order_by}"

    def build_count_query(self) -> str:
        return f"SELECT COUNT(*) FROM {self.table.to_string()} {self.build_where_part()}"
