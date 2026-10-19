"""Structured tool inputs. Their field descriptions become the JSON schema the assistant sees."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from ..core.filters import FILTER_OPERATORS

FilterOp = Literal[tuple(key for key, _label, _n in FILTER_OPERATORS)]  # type: ignore[valid-type]
Scalar = Union[str, int, float, bool, None]


class RowFilter(BaseModel):
    """One condition; several conditions are combined with AND."""
    model_config = ConfigDict(extra="forbid")

    column: str = Field(description="Column name")
    op: FilterOp = Field(description="contains / starts / ends (text, % and _ are wildcards), equals, not_equals, "
                                     "gt, ge, lt, le, between (numbers: value..value2), regex, "
                                     "empty / not_empty (empty text), null / not_null")
    value: Scalar = Field(default=None, description="Value to compare with (not needed for empty/null ops)")
    value2: Scalar = Field(default=None, description="Upper bound, only for between")


class SortKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    descending: bool = False


class ColumnSpec(BaseModel):
    """A column of a new table."""
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = Field(default="TEXT", description="INTEGER, REAL, TEXT, BLOB, NUMERIC (or any SQLite type name)")
    primary_key: bool = Field(default=False, description="Part of the PRIMARY KEY")
    autoincrement: bool = Field(default=False, description="AUTOINCREMENT (single INTEGER primary key only)")
    not_null: bool = False
    unique: bool = False
    default: Optional[str] = Field(default=None, description="Default as an SQL expression, e.g. 0, 'new', "
                                                             "CURRENT_TIMESTAMP")
    check: Optional[str] = Field(default=None, description="CHECK constraint expression, e.g. price >= 0")
    references: Optional[str] = Field(default=None, description="Foreign key target as table or table(column)")
