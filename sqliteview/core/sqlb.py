"""Schema object model.

Port of upstream ``src/sql/sqlitetypes.{h,cpp}`` and ``src/sql/ObjectIdentifier.{h,cpp}``.
The class layout, SQL generation and helper semantics follow the C++ original.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field as dc_field
from enum import IntEnum
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Identifier quoting
# ---------------------------------------------------------------------------


class EscapeQuoting(IntEnum):
    DoubleQuotes = 0
    GraveAccents = 1
    SquareBrackets = 2


_custom_quoting = EscapeQuoting.DoubleQuotes


def set_identifier_quoting(quoting: EscapeQuoting) -> None:
    global _custom_quoting
    _custom_quoting = EscapeQuoting(quoting)


def identifier_quote_char() -> str:
    return {EscapeQuoting.GraveAccents: "`", EscapeQuoting.SquareBrackets: "["}.get(_custom_quoting, '"')


def escape_identifier(identifier: str) -> str:
    if _custom_quoting == EscapeQuoting.GraveAccents:
        return "`" + identifier.replace("`", "``") + "`"
    if _custom_quoting == EscapeQuoting.SquareBrackets:
        # No escaping possible inside square brackets (same as upstream)
        return "[" + identifier + "]"
    return '"' + identifier.replace('"', '""') + '"'


def escape_identifiers(ids: Iterable[str]) -> list[str]:
    return [escape_identifier(i) for i in ids]


def escape_string(literal: str) -> str:
    return "'" + literal.replace("'", "''") + "'"


def escape_value(value) -> str:
    """SQL literal for a Python value (upstream ``escapeByteArray`` extended to typed values)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        from .data import is_text_only

        if is_text_only(b):
            return escape_string(b.decode("utf-8"))
        return "X'" + b.hex().upper() + "'"
    return escape_string(str(value))


def join(vec: Iterable[str], delim: str) -> str:
    return delim.join(vec)


def starts_with_ci(s: str, prefix: str) -> bool:
    return s[: len(prefix)].lower() == prefix.lower()


# ---------------------------------------------------------------------------
# ObjectIdentifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectIdentifier:
    schema: str = "main"
    name: str = ""

    def is_empty(self) -> bool:
        return not self.name

    def to_string(self, short_name: bool = False) -> str:
        if short_name and self.schema == "main":
            return escape_identifier(self.name)
        return escape_identifier(self.schema) + "." + escape_identifier(self.name)

    def to_display_string(self) -> str:
        return self.name if self.schema == "main" else f"{self.schema}.{self.name}"

    def to_serialised(self) -> str:
        return f"{len(self.schema)},{len(self.name)}:{self.schema}{self.name}"

    @staticmethod
    def from_serialised(serialised: str) -> Optional["ObjectIdentifier"]:
        try:
            sizes, rest = serialised.split(":", 1)
            s_schema, s_name = (int(x) for x in sizes.split(","))
        except ValueError:
            return None
        if s_schema + s_name != len(rest):
            return None
        return ObjectIdentifier(rest[:s_schema], rest[s_schema:])


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def _constraint_prefix(name: str) -> str:
    return f"CONSTRAINT {escape_identifier(name)} " if name else ""


@dataclass
class ForeignKeyClause:
    table: str = ""
    columns: list[str] = dc_field(default_factory=list)
    constraint: str = ""  # e.g. "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED"
    name: str = ""

    def to_string(self) -> str:
        result = escape_identifier(self.table)
        if self.columns:
            result += "(" + ",".join(escape_identifiers(self.columns)) + ")"
        if self.constraint:
            result += " " + self.constraint
        return result

    def to_sql(self, columns: list[str]) -> str:
        return (_constraint_prefix(self.name) + "FOREIGN KEY(" + ",".join(escape_identifiers(columns))
                + ") REFERENCES " + self.to_string())


@dataclass
class UniqueConstraint:
    conflict_action: str = ""
    name: str = ""

    def is_primary_key(self) -> bool:
        return False

    def to_sql(self, columns: list["IndexedColumn"]) -> str:
        result = _constraint_prefix(self.name) + "UNIQUE"
        if columns:
            result += "(" + join_indexed_columns(columns, ",") + ")"
        if self.conflict_action:
            result += " ON CONFLICT " + self.conflict_action
        return result


@dataclass
class PrimaryKeyConstraint(UniqueConstraint):
    auto_increment: bool = False

    def is_primary_key(self) -> bool:
        return True

    def to_sql(self, columns: list["IndexedColumn"]) -> str:
        result = (_constraint_prefix(self.name) + "PRIMARY KEY(" + join_indexed_columns(columns, ",")
                  + (" AUTOINCREMENT" if self.auto_increment else "") + ")")
        if self.conflict_action:
            result += " ON CONFLICT " + self.conflict_action
        return result


@dataclass
class NotNullConstraint:
    conflict_action: str = ""
    name: str = ""

    def to_sql(self) -> str:
        result = _constraint_prefix(self.name) + "NOT NULL"
        if self.conflict_action:
            result += " ON CONFLICT " + self.conflict_action
        return result


@dataclass
class CheckConstraint:
    expression: str = ""
    name: str = ""

    def to_sql(self) -> str:
        return _constraint_prefix(self.name) + "CHECK(" + self.expression + ")"


@dataclass
class DefaultConstraint:
    value: str = ""
    name: str = ""

    def to_sql(self) -> str:
        return _constraint_prefix(self.name) + "DEFAULT " + self.value


@dataclass
class CollateConstraint:
    collation: str = ""
    name: str = ""

    def to_sql(self) -> str:
        return _constraint_prefix(self.name) + "COLLATE " + self.collation


@dataclass
class GeneratedColumnConstraint:
    expression: str = ""
    storage_: str = "VIRTUAL"
    name: str = ""

    @property
    def storage(self) -> str:
        return self.storage_ or "VIRTUAL"

    def to_sql(self) -> str:
        return _constraint_prefix(self.name) + f"GENERATED ALWAYS AS ({self.expression}) {self.storage}"


# ---------------------------------------------------------------------------
# Field
# ---------------------------------------------------------------------------

_TEXT_PREFIXES = ("character", "varchar", "varying character", "nchar", "native character", "nvarchar")
_INTEGER_TYPES = {"int", "integer", "tinyint", "smallint", "mediumint", "bigint", "unsigned big int", "int2", "int8"}
_REAL_TYPES = {"real", "double", "double precision", "float"}
_NUMERIC_TYPES = {"numeric", "boolean", "date", "datetime"}


class Affinity(IntEnum):
    Integer = 1
    Float = 2
    Text = 3
    Blob = 4


@dataclass
class Field:
    name: str = ""
    type: str = ""
    notnull_: Optional[NotNullConstraint] = None
    check_: Optional[CheckConstraint] = None
    default_: Optional[DefaultConstraint] = None
    unique_: Optional[UniqueConstraint] = None
    collation_: Optional[CollateConstraint] = None
    generated: Optional[GeneratedColumnConstraint] = None

    @staticmethod
    def make(name: str, type_: str, notnull: bool = False, default: str = "", check: str = "",
             unique: bool = False, collation: str = "") -> "Field":
        f = Field(name, type_)
        f.set_notnull(notnull)
        f.set_default(default)
        f.set_check(check)
        f.set_unique(unique)
        f.set_collation(collation)
        return f

    # --- accessors mirroring the C++ API
    @property
    def notnull(self) -> bool:
        return self.notnull_ is not None

    @property
    def check(self) -> str:
        return self.check_.expression if self.check_ else ""

    @property
    def default_value(self) -> str:
        return self.default_.value if self.default_ else ""

    @property
    def unique(self) -> bool:
        return self.unique_ is not None

    @property
    def collation(self) -> str:
        return self.collation_.collation if self.collation_ else ""

    def set_notnull(self, v: bool = True) -> None:
        self.notnull_ = NotNullConstraint() if v else None

    def set_check(self, expr: str) -> None:
        self.check_ = CheckConstraint(expr) if expr else None

    def set_default(self, value: str) -> None:
        self.default_ = DefaultConstraint(value) if value else None

    def set_unique(self, v: bool) -> None:
        self.unique_ = UniqueConstraint() if v else None

    def set_collation(self, c: str) -> None:
        self.collation_ = CollateConstraint(c) if c else None

    def to_string(self, indent: str = "\t", sep: str = "\t") -> str:
        s = indent + escape_identifier(self.name) + sep + self.type
        if self.notnull_:
            s += " " + self.notnull_.to_sql()
        if self.default_:
            s += " " + self.default_.to_sql()
        if self.check_:
            s += " " + self.check_.to_sql()
        if self.unique_:
            s += " " + self.unique_.to_sql([])
        if self.collation_:
            s += " " + self.collation_.to_sql()
        if self.generated:
            s += " " + self.generated.to_sql()
        return s

    # --- type helpers (same rules as upstream)
    def is_text(self) -> bool:
        t = self.type.lower()
        return t.startswith(_TEXT_PREFIXES) or t in ("text", "clob")

    def is_integer(self) -> bool:
        return self.type.lower() in _INTEGER_TYPES

    def is_real(self) -> bool:
        return self.type.lower() in _REAL_TYPES

    def is_numeric(self) -> bool:
        t = self.type.lower()
        return t.startswith("decimal") or t in _NUMERIC_TYPES

    def is_blob(self) -> bool:
        return not self.type or self.type.lower() == "blob"

    def affinity(self) -> Affinity:
        if self.is_integer():
            return Affinity.Integer
        if self.is_text():
            return Affinity.Text
        if self.is_blob():
            return Affinity.Blob
        if self.is_real() or self.is_numeric():
            return Affinity.Float
        return Affinity.Blob


# ---------------------------------------------------------------------------
# Indexed column
# ---------------------------------------------------------------------------


@dataclass
class IndexedColumn:
    name: str = ""
    is_expression: bool = False
    order: str = ""

    def matches(self, name: str) -> bool:
        return not self.is_expression and self.name == name

    def to_string(self, indent: str = "\t", sep: str = "\t") -> str:
        n = self.name if self.is_expression else escape_identifier(self.name)
        return indent + n + ((sep + self.order) if self.order else "")


def join_indexed_columns(cols: list[IndexedColumn], delim: str) -> str:
    out = []
    for i, c in enumerate(cols):
        out.append(c.to_string("", " ") if i == 0 else c.to_string("", " "))
    return delim.join(out)


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


class Object:
    kind = "object"

    def __init__(self, name: str = ""):
        self.name = name
        self.original_sql = ""
        self.fully_parsed = False

    def sql(self, schema: str = "main", if_not_exists: bool = False) -> str:  # pragma: no cover
        raise NotImplementedError

    def clone(self):
        return copy.deepcopy(self)

    def base_table(self) -> str:
        return ""


class Table(Object):
    kind = "table"

    def __init__(self, name: str = ""):
        super().__init__(name)
        self.fields: list[Field] = []
        # (columns, constraint) pairs; stores PRIMARY KEY and UNIQUE table constraints
        self.index_constraints: list[tuple[list[IndexedColumn], UniqueConstraint]] = []
        self.foreign_keys: list[tuple[list[str], ForeignKeyClause]] = []
        self.check_constraints: list[CheckConstraint] = []
        self.virtual_using = ""
        self.without_rowid = False
        self.strict = False

    def __eq__(self, other) -> bool:
        if not isinstance(other, Table):
            return NotImplemented
        return (self.name == other.name and self.fully_parsed == other.fully_parsed
                and self.without_rowid == other.without_rowid and self.strict == other.strict
                and self.virtual_using == other.virtual_using and self.fields == other.fields
                and self.index_constraints == other.index_constraints
                and self.foreign_keys == other.foreign_keys
                and self.check_constraints == other.check_constraints)

    __hash__ = None  # type: ignore[assignment]

    def is_view(self) -> bool:
        return False

    def is_virtual(self) -> bool:
        return bool(self.virtual_using)

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def find_field(self, name: str) -> Optional[Field]:
        lname = name.lower()
        for f in self.fields:
            if f.name.lower() == lname:
                return f
        return None

    def field_index(self, name: str) -> int:
        lname = name.lower()
        for i, f in enumerate(self.fields):
            if f.name.lower() == lname:
                return i
        return -1

    def remove_field(self, name: str) -> bool:
        i = self.field_index(name)
        if i < 0:
            return False
        del self.fields[i]
        return True

    def rowid_columns(self) -> list[str]:
        if self.without_rowid:
            return [c.name for c in self.primary_key_columns()]
        return ["_rowid_"]

    # --- constraints
    def primary_key(self) -> Optional[PrimaryKeyConstraint]:
        for _, c in self.index_constraints:
            if c.is_primary_key():
                return c  # type: ignore[return-value]
        return None

    def primary_key_columns(self) -> list[IndexedColumn]:
        for cols, c in self.index_constraints:
            if c.is_primary_key():
                return cols
        return []

    def primary_key_names(self) -> list[str]:
        return [c.name for c in self.primary_key_columns()]

    def add_index_constraint(self, columns: list, constraint: UniqueConstraint) -> None:
        cols = [c if isinstance(c, IndexedColumn) else IndexedColumn(c, False) for c in columns]
        self.index_constraints.append((cols, constraint))

    def add_foreign_key(self, columns: list, fk: ForeignKeyClause) -> None:
        cols = [c.name if isinstance(c, IndexedColumn) else c for c in columns]
        self.foreign_keys.append((cols, fk))

    def remove_constraint(self, constraint) -> None:
        if isinstance(constraint, CheckConstraint):
            self.check_constraints = [c for c in self.check_constraints if c is not constraint]
        elif isinstance(constraint, ForeignKeyClause):
            self.foreign_keys = [e for e in self.foreign_keys if e[1] is not constraint]
        else:
            self.index_constraints = [e for e in self.index_constraints if e[1] is not constraint]

    def foreign_key(self, columns: list[str]) -> Optional[ForeignKeyClause]:
        for cols, fk in self.foreign_keys:
            if cols == columns:
                return fk
        return None

    def set_primary_key_columns(self, names: list[str], auto_increment: Optional[bool] = None) -> None:
        """Replace the PK column list (keeping its other properties); drops the PK when empty."""
        pk = self.primary_key()
        if not names:
            if pk is not None:
                self.remove_constraint(pk)
            return
        if pk is None:
            pk = PrimaryKeyConstraint()
            self.index_constraints.insert(0, ([IndexedColumn(n, False) for n in names], pk))
        else:
            for i, (cols, c) in enumerate(self.index_constraints):
                if c is pk:
                    old = {ic.name: ic for ic in cols}
                    self.index_constraints[i] = ([old.get(n, IndexedColumn(n, False)) for n in names], pk)
        if auto_increment is not None:
            pk.auto_increment = auto_increment

    def remove_key_from_all_constraints(self, key: str) -> None:
        new_ic = []
        for cols, c in self.index_constraints:
            nc = [x for x in cols if not x.matches(key)]
            if nc:
                new_ic.append((nc, c))
        self.index_constraints = new_ic
        new_fk = []
        for cols, fk in self.foreign_keys:
            nc = [x for x in cols if x != key]
            if nc:
                new_fk.append((nc, fk))
        self.foreign_keys = new_fk

    def rename_key_in_all_constraints(self, key: str, to: str) -> None:
        if key == to:
            return
        for cols, _ in self.index_constraints:
            for c in cols:
                if c.matches(key):
                    c.name = to
        self.foreign_keys = [([to if x == key else x for x in cols], fk) for cols, fk in self.foreign_keys]

    # --- SQL
    def sql(self, schema: str = "main", if_not_exists: bool = False) -> str:
        if self.is_virtual():
            return ("CREATE VIRTUAL TABLE " + ObjectIdentifier(schema, self.name).to_string(True)
                    + " USING " + self.virtual_using + ";")
        s = "CREATE TABLE "
        if if_not_exists:
            s += "IF NOT EXISTS "
        s += ObjectIdentifier(schema, self.name).to_string(True) + " (\n"
        s += ",\n".join(f.to_string() for f in self.fields)
        for cols, c in self.index_constraints:
            s += ",\n\t" + c.to_sql(cols)
        for cols, fk in self.foreign_keys:
            s += ",\n\t" + fk.to_sql(cols)
        for c in self.check_constraints:
            s += ",\n\t" + c.to_sql()
        s += "\n)"
        opts = []
        if self.without_rowid:
            opts.append("WITHOUT ROWID")
        if self.strict:
            opts.append("STRICT")
        if opts:
            s += " " + ",".join(opts)
        return s + ";"


class View(Table):
    kind = "view"

    def is_view(self) -> bool:
        return True

    def sql(self, schema: str = "main", if_not_exists: bool = False) -> str:
        return self.original_sql


class Index(Object):
    kind = "index"

    def __init__(self, name: str = ""):
        super().__init__(name)
        self.fields: list[IndexedColumn] = []
        self.unique = False
        self.table = ""
        self.where_expr = ""

    def base_table(self) -> str:
        return self.table

    def find_field(self, name: str) -> Optional[IndexedColumn]:
        for c in self.fields:
            if c.name.lower() == name.lower():
                return c
        return None

    def remove_field(self, name: str) -> bool:
        for i, c in enumerate(self.fields):
            if c.name.lower() == name.lower():
                del self.fields[i]
                return True
        return False

    def sql(self, schema: str = "main", if_not_exists: bool = False) -> str:
        s = "CREATE UNIQUE INDEX " if self.unique else "CREATE INDEX "
        if if_not_exists:
            s += "IF NOT EXISTS "
        s += ObjectIdentifier(schema, self.name).to_string(True)
        s += " ON " + escape_identifier(self.table) + " (\n"
        s += ",\n".join(c.to_string() for c in self.fields)
        s += "\n)"
        if self.where_expr:
            s += " WHERE " + self.where_expr
        return s + ";"


class Trigger(Object):
    kind = "trigger"

    def __init__(self, name: str = ""):
        super().__init__(name)
        self.table = ""

    def base_table(self) -> str:
        return self.table

    def sql(self, schema: str = "main", if_not_exists: bool = False) -> str:
        return self.original_sql
