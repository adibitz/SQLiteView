"""Filter expression -> SQL condition. Port of upstream ``CondFormat::filterToSqlCondition``
and ``GlobalFilter::tokenize``.

Filter syntax (identical to DB4S):
    text      -> LIKE '%text%'   (unless the text already contains %)
    =val      -> = 'val'         (=NULL -> IS NULL)
    <>val     -> <> 'val'        (<>NULL -> IS NOT NULL, <>'' -> <> '')
    >val, <val, >=val, <=val
    a~b       -> BETWEEN a AND b (numeric, a < b)
    /regex/   -> REGEXP 'regex'
"""
from __future__ import annotations

from .sqlb import escape_string


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def filter_to_sql_condition(value: str, escape_character: str = "\\") -> str:
    if not value:
        return ""
    op = "LIKE"
    val = val2 = ""
    escape = ""
    numeric = False
    ok = False

    if "~" in value:
        idx = value.index("~")
        val, val2 = value[:idx], value[idx + 1:]
        if _is_number(val) and _is_number(val2):
            ok = float(val) < float(val2)
    if ok:
        op = "BETWEEN"
        numeric = True
    else:
        val = val2 = ""
        head2 = value[:2]
        if head2 in (">=", "<=", "<>"):
            if head2 == "<>" and value[2:] == "NULL":
                op, numeric, val = "IS NOT", True, "NULL"
            elif head2 == "<>" and value[2:] == "''":
                op, numeric, val = "<>", True, "''"
            else:
                op = head2
                val = value[2:]
                numeric = _is_number(val)
        elif value[0] in "<>":
            op = value[0]
            val = value[1:]
            numeric = _is_number(val)
        elif value[0] == "=":
            val = value[1:]
            if val != "NULL":
                op = "="
            else:
                op, numeric = "IS", True
        elif value[0] == "/" and value[-1] == "/" and len(value) > 2:
            val = value[1:-1]
            op = "REGEXP"
        else:
            esc = escape_character
            if esc == "'":
                esc = "''"
            if esc:
                escape = f"ESCAPE '{esc}'"
            if "%" not in value:
                val = "%" + value + "%"

    if not val:
        val = value
    if not val or val in ("%", "%%"):
        return ""
    if not numeric and val != "''":
        val = escape_string(val)
    clause = f"{op} {val}"
    if val2:
        clause += f" AND {val2}"
    if escape:
        clause += " " + escape
    return clause


def _legacy_tokenize(value: str) -> list[str]:
    return [v for v in value.strip().split(" ") if v]


def tokenize_global_filter(value: str) -> list[str]:
    """Split a global filter into terms; /regex with spaces/ stays one term."""
    filters: list[str] = []
    pos, n = 0, len(value)
    while pos < n:
        while pos < n and value[pos] == " ":
            pos += 1
        if pos == n:
            break
        start = pos
        if value[start] != "/":
            while pos < n and value[pos] != " ":
                pos += 1
            filters.append(value[start:pos])
            continue
        found = False
        pos = start + 1
        while pos < n:
            if value[pos] != "/":
                pos += 1
                continue
            backslashes = 0
            p = pos - 1
            while p >= start and value[p] == "\\":
                backslashes += 1
                p -= 1
            unescaped = backslashes % 2 == 0
            at_end = pos + 1 == n or value[pos + 1] == " "
            if unescaped and not at_end:
                return _legacy_tokenize(value)
            if unescaped:
                filters.append(value[start:pos + 1])
                pos += 1
                found = True
                break
            pos += 1
        if not found:
            return _legacy_tokenize(value)
    return filters
