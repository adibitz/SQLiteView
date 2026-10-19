"""Recursive-descent parser for SQLite CREATE TABLE / CREATE VIRTUAL TABLE / CREATE INDEX.

Replaces the bison-generated parser in upstream ``src/sql/parser``. It produces the same
object model (see ``sqlb``): column-level PRIMARY KEY / REFERENCES constraints are lifted
into table constraints, exactly like the upstream parser does, so regenerated SQL matches.
When a statement cannot be parsed, ``fully_parsed`` stays False and callers fall back to
``PRAGMA table_info`` (same strategy as upstream ``DBBrowserDB::updateSchema``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import sqlb

# --------------------------------------------------------------------------- tokenizer

ID, STR, NUM, BLOB, OP, EOF = "id", "str", "num", "blob", "op", "eof"

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lc>--[^\n]*)
  | (?P<bc>/\*.*?(?:\*/|\Z))
  | (?P<blob>[xX]'(?:[^']|'')*')
  | (?P<str>'(?:[^']|'')*')
  | (?P<dq>"(?:[^"]|"")*")
  | (?P<bt>`(?:[^`]|``)*`)
  | (?P<br>\[[^\]]*\])
  | (?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|0[xX][0-9a-fA-F]+)
  | (?P<id>[A-Za-z_\u0080-￿][A-Za-z0-9_$\u0080-￿]*)
  | (?P<op>\|\||<<|>>|<=|>=|==|!=|<>|->>|->|[-+*/%&|~<>=(),;.])
  """,
    re.VERBOSE | re.DOTALL,
)


@dataclass
class Token:
    kind: str
    text: str  # raw text
    value: str  # unquoted identifier / upper-case keyword candidate
    start: int
    end: int

    def is_kw(self, *words: str) -> bool:
        return self.kind == ID and self.value.upper() in words


class ParseError(Exception):
    pass


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    n = len(sql)
    while pos < n:
        m = _TOKEN_RE.match(sql, pos)
        if not m:
            raise ParseError(f"Unexpected character {sql[pos]!r} at {pos}")
        kind = m.lastgroup
        text = m.group(0)
        if kind in ("ws", "lc", "bc"):
            pass
        elif kind == "dq":
            tokens.append(Token(ID, text, text[1:-1].replace('""', '"'), m.start(), m.end()))
        elif kind == "bt":
            tokens.append(Token(ID, text, text[1:-1].replace("``", "`"), m.start(), m.end()))
        elif kind == "br":
            tokens.append(Token(ID, text, text[1:-1], m.start(), m.end()))
        elif kind == "id":
            tokens.append(Token(ID, text, text, m.start(), m.end()))
        elif kind == "str":
            tokens.append(Token(STR, text, text[1:-1].replace("''", "'"), m.start(), m.end()))
        elif kind == "blob":
            tokens.append(Token(BLOB, text, text, m.start(), m.end()))
        elif kind == "num":
            tokens.append(Token(NUM, text, text, m.start(), m.end()))
        else:
            tokens.append(Token(OP, text, text, m.start(), m.end()))
        pos = m.end()
    tokens.append(Token(EOF, "", "", n, n))
    return tokens


# Keywords that start a column constraint (they terminate the type name)
_COLUMN_CONSTRAINT_START = {
    "CONSTRAINT", "PRIMARY", "NOT", "NULL", "UNIQUE", "CHECK", "DEFAULT", "COLLATE",
    "REFERENCES", "GENERATED", "AS",
}
_TABLE_CONSTRAINT_START = {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}
_CONFLICT_ACTIONS = {"ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE"}


class _Parser:
    def __init__(self, sql: str):
        self.sql = sql
        self.toks = tokenize(sql)
        self.i = 0

    # --- primitives
    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self) -> Token:
        t = self.toks[self.i]
        if t.kind != EOF:
            self.i += 1
        return t

    def accept_kw(self, *words: str) -> bool:
        if self.cur.is_kw(*words):
            self.advance()
            return True
        return False

    def expect_kw(self, *words: str) -> Token:
        if not self.cur.is_kw(*words):
            raise ParseError(f"Expected {'/'.join(words)} but got {self.cur.text!r}")
        return self.advance()

    def accept_op(self, op: str) -> bool:
        if self.cur.kind == OP and self.cur.text == op:
            self.advance()
            return True
        return False

    def expect_op(self, op: str) -> Token:
        if not (self.cur.kind == OP and self.cur.text == op):
            raise ParseError(f"Expected {op!r} but got {self.cur.text!r}")
        return self.advance()

    def name(self) -> str:
        t = self.cur
        if t.kind in (ID, STR):
            self.advance()
            return t.value
        raise ParseError(f"Expected a name but got {t.text!r}")

    def qualified_name(self) -> tuple[str, str]:
        first = self.name()
        if self.accept_op("."):
            return first, self.name()
        return "", first

    def balanced_text(self) -> str:
        """Consume '( ... )' and return the raw text between the parentheses."""
        open_tok = self.expect_op("(")
        depth = 1
        while depth:
            t = self.advance()
            if t.kind == EOF:
                raise ParseError("Unbalanced parentheses")
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth -= 1
        close_tok = self.toks[self.i - 1]
        return self.sql[open_tok.end:close_tok.start].strip()

    def raw_until(self, stop) -> str:
        """Consume tokens until ``stop(token)`` is true at paren depth 0; return raw text."""
        start = self.cur.start
        end = start
        depth = 0
        while self.cur.kind != EOF:
            t = self.cur
            if depth == 0 and stop(t):
                break
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                if depth == 0:
                    break
                depth -= 1
            end = t.end
            self.advance()
        return self.sql[start:end].strip()

    def conflict_clause(self) -> str:
        if self.cur.is_kw("ON") and self.peek().is_kw("CONFLICT"):
            self.advance()
            self.advance()
            t = self.advance()
            if t.value.upper() not in _CONFLICT_ACTIONS:
                raise ParseError("Invalid conflict action")
            return t.value.upper()
        return ""

    # --- indexed columns
    def indexed_column_list(self, allow_autoinc: bool = False) -> tuple[list[sqlb.IndexedColumn], bool]:
        self.expect_op("(")
        cols: list[sqlb.IndexedColumn] = []
        autoinc = False
        while True:
            start_i = self.i
            expr = self.raw_until(lambda t: (t.kind == OP and t.text == ",")
                                  or t.is_kw("ASC", "DESC", "COLLATE", "AUTOINCREMENT"))
            span = self.toks[start_i:self.i]
            if len(span) == 1 and span[0].kind in (ID, STR):
                col = sqlb.IndexedColumn(span[0].value, False)
            else:
                col = sqlb.IndexedColumn(expr, True)
            if self.accept_kw("COLLATE"):
                coll = self.name()
                if not col.is_expression:
                    col = sqlb.IndexedColumn(sqlb.escape_identifier(col.name) + " COLLATE " + coll, True)
                else:
                    col.name += " COLLATE " + coll
            if self.cur.is_kw("ASC", "DESC"):
                col.order = self.advance().value.upper()
            if allow_autoinc and self.accept_kw("AUTOINCREMENT"):
                autoinc = True
            cols.append(col)
            if self.accept_op(","):
                continue
            self.expect_op(")")
            return cols, autoinc

    def name_list(self) -> list[str]:
        self.expect_op("(")
        names = [self.name()]
        while self.accept_op(","):
            names.append(self.name())
        self.expect_op(")")
        return names

    # --- foreign key clause
    def foreign_key_clause(self) -> sqlb.ForeignKeyClause:
        self.expect_kw("REFERENCES")
        fk = sqlb.ForeignKeyClause(self.name())
        if self.cur.kind == OP and self.cur.text == "(":
            fk.columns = self.name_list()

        def stop(t: Token) -> bool:
            if t.kind == OP and t.text == ",":
                return True
            # A new column constraint can follow the FK clause
            return t.is_kw("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "DEFAULT", "COLLATE",
                           "REFERENCES", "GENERATED", "AS") or (t.is_kw("NOT") and not self.peek().is_kw("DEFERRABLE")) \
                or (t.is_kw("NULL"))

        fk.constraint = re.sub(r"\s+", " ", self.raw_until(stop))
        return fk

    # --- CREATE TABLE
    def parse_table(self) -> sqlb.Table:
        self.expect_kw("CREATE")
        self.accept_kw("TEMP", "TEMPORARY")
        if self.accept_kw("VIRTUAL"):
            self.expect_kw("TABLE")
            self._if_not_exists()
            _, name = self.qualified_name()
            self.expect_kw("USING")
            using = self.sql[self.cur.start:].strip().rstrip(";").strip()
            t = sqlb.Table(name)
            t.virtual_using = using
            t.fully_parsed = True
            return t
        self.expect_kw("TABLE")
        self._if_not_exists()
        _, name = self.qualified_name()
        table = sqlb.Table(name)
        if self.cur.is_kw("AS"):
            raise ParseError("CREATE TABLE ... AS SELECT is not structurally parsed")
        self.expect_op("(")
        while True:
            if self.cur.kind == ID and self.cur.value.upper() in _TABLE_CONSTRAINT_START and \
                    not self._looks_like_column_def():
                self.table_constraint(table)
            else:
                self.column_def(table)
            if self.accept_op(","):
                continue
            self.expect_op(")")
            break
        # Table options
        while self.cur.kind != EOF:
            if self.accept_kw("WITHOUT"):
                if not self.cur.is_kw("ROWID"):
                    raise ParseError("Expected ROWID")
                self.advance()
                table.without_rowid = True
            elif self.accept_kw("STRICT"):
                table.strict = True
            elif self.accept_op(",") or self.accept_op(";"):
                continue
            else:
                raise ParseError(f"Unexpected token {self.cur.text!r} after table definition")
        table.fully_parsed = True
        return table

    def _looks_like_column_def(self) -> bool:
        # e.g. a column named "check" written unquoted would be ambiguous; a table constraint keyword
        # is followed by "(" (CHECK/UNIQUE), KEY (PRIMARY/FOREIGN) or a name (CONSTRAINT).
        t, nxt = self.cur, self.peek()
        kw = t.value.upper()
        if t.text != t.value:  # quoted identifier -> column
            return True
        if kw in ("CHECK", "UNIQUE"):
            return not (nxt.kind == OP and nxt.text == "(")
        if kw in ("PRIMARY", "FOREIGN"):
            return not nxt.is_kw("KEY")
        return False

    def _if_not_exists(self) -> None:
        if self.cur.is_kw("IF"):
            self.advance()
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")

    def column_def(self, table: sqlb.Table) -> None:
        f = sqlb.Field(self.name(), "")
        # type name
        type_parts: list[str] = []
        type_start = self.cur.start
        type_end = type_start
        while self.cur.kind in (ID, STR) and not (self.cur.kind == ID and self.cur.value.upper() in _COLUMN_CONSTRAINT_START):
            type_end = self.advance().end
            type_parts.append("x")
        if type_parts and self.cur.kind == OP and self.cur.text == "(":
            self.balanced_text()
            type_end = self.toks[self.i - 1].end
        if type_parts:
            f.type = re.sub(r"\s+", " ", self.sql[type_start:type_end].strip())
        # column constraints
        while True:
            cname = ""
            if self.accept_kw("CONSTRAINT"):
                cname = self.name()
            t = self.cur
            if t.is_kw("PRIMARY"):
                self.advance()
                self.expect_kw("KEY")
                order = self.advance().value.upper() if self.cur.is_kw("ASC", "DESC") else ""
                pk = sqlb.PrimaryKeyConstraint(name=cname)
                pk.conflict_action = self.conflict_clause()
                if self.accept_kw("AUTOINCREMENT"):
                    pk.auto_increment = True
                table.add_index_constraint([sqlb.IndexedColumn(f.name, False, order)], pk)
            elif t.is_kw("NOT"):
                self.advance()
                self.expect_kw("NULL")
                f.notnull_ = sqlb.NotNullConstraint(self.conflict_clause(), cname)
            elif t.is_kw("NULL"):
                self.advance()
                self.conflict_clause()
            elif t.is_kw("UNIQUE"):
                self.advance()
                f.unique_ = sqlb.UniqueConstraint(self.conflict_clause(), cname)
            elif t.is_kw("CHECK"):
                self.advance()
                f.check_ = sqlb.CheckConstraint(self.balanced_text(), cname)
            elif t.is_kw("DEFAULT"):
                self.advance()
                f.default_ = sqlb.DefaultConstraint(self.default_value(), cname)
            elif t.is_kw("COLLATE"):
                self.advance()
                f.collation_ = sqlb.CollateConstraint(self.name(), cname)
            elif t.is_kw("REFERENCES"):
                fk = self.foreign_key_clause()
                fk.name = cname
                table.add_foreign_key([f.name], fk)
            elif t.is_kw("GENERATED", "AS"):
                if self.accept_kw("GENERATED"):
                    self.expect_kw("ALWAYS")
                self.expect_kw("AS")
                expr = self.balanced_text()
                storage = ""
                if self.cur.is_kw("STORED", "VIRTUAL"):
                    storage = self.advance().value.upper()
                f.generated = sqlb.GeneratedColumnConstraint(expr, storage or "VIRTUAL", cname)
            else:
                if cname:
                    raise ParseError("Dangling CONSTRAINT name")
                break
        table.fields.append(f)

    def default_value(self) -> str:
        t = self.cur
        if t.kind == OP and t.text == "(":
            return "(" + self.balanced_text() + ")"
        if t.kind == OP and t.text in ("+", "-"):
            self.advance()
            num = self.advance()
            return t.text + num.text
        self.advance()
        return t.text

    def table_constraint(self, table: sqlb.Table) -> None:
        cname = ""
        if self.accept_kw("CONSTRAINT"):
            cname = self.name()
        if self.accept_kw("PRIMARY"):
            self.expect_kw("KEY")
            cols, autoinc = self.indexed_column_list(allow_autoinc=True)
            pk = sqlb.PrimaryKeyConstraint(name=cname, auto_increment=autoinc)
            pk.conflict_action = self.conflict_clause()
            table.add_index_constraint(cols, pk)
        elif self.accept_kw("UNIQUE"):
            cols, _ = self.indexed_column_list()
            uc = sqlb.UniqueConstraint(name=cname)
            uc.conflict_action = self.conflict_clause()
            table.add_index_constraint(cols, uc)
        elif self.accept_kw("CHECK"):
            table.check_constraints.append(sqlb.CheckConstraint(self.balanced_text(), cname))
        elif self.accept_kw("FOREIGN"):
            self.expect_kw("KEY")
            cols = self.name_list()
            fk = self.foreign_key_clause()
            fk.name = cname
            table.add_foreign_key(cols, fk)
        else:
            raise ParseError(f"Unknown table constraint {self.cur.text!r}")

    # --- CREATE INDEX
    def parse_index(self) -> sqlb.Index:
        self.expect_kw("CREATE")
        idx = sqlb.Index()
        if self.accept_kw("UNIQUE"):
            idx.unique = True
        self.expect_kw("INDEX")
        self._if_not_exists()
        _, idx.name = self.qualified_name()
        self.expect_kw("ON")
        idx.table = self.name()
        idx.fields, _ = self.indexed_column_list()
        if self.accept_kw("WHERE"):
            idx.where_expr = self.sql[self.cur.start:].strip().rstrip(";").strip()
        else:
            self.accept_op(";")
            if self.cur.kind != EOF:
                raise ParseError("Unexpected trailing tokens")
        idx.fully_parsed = True
        return idx


def parse_table(sql: str) -> sqlb.Table:
    try:
        t = _Parser(sql).parse_table()
    except (ParseError, IndexError):
        t = sqlb.Table("")
        t.fully_parsed = False
    t.original_sql = sql
    return t


def parse_index(sql: str) -> sqlb.Index:
    try:
        i = _Parser(sql).parse_index()
    except (ParseError, IndexError):
        i = sqlb.Index("")
        i.fully_parsed = False
    i.original_sql = sql
    return i


def parse_view(sql: str) -> sqlb.View:
    v = sqlb.View("")
    v.original_sql = sql
    return v


def parse_trigger(sql: str) -> sqlb.Trigger:
    t = sqlb.Trigger("")
    t.original_sql = sql
    return t


def first_keyword(sql: str) -> Optional[str]:
    """Upper-case first keyword of a statement, ignoring leading whitespace and comments."""
    try:
        for t in tokenize(sql[:4096]):
            return t.value.upper() if t.kind == ID else None
    except ParseError:
        m = re.match(r"\s*([A-Za-z]+)", sql)
        return m.group(1).upper() if m else None
    return None
