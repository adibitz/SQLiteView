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


# --------------------------------------------------------------------------- filter builder
# Operators offered by the filter builder, each producing text in the filter syntax above.
FILTER_OPERATORS: list[tuple[str, str, int]] = [  # key, label, number of values
    ("contains", "contains", 1),
    ("equals", "equals", 1),
    ("not_equals", "does not equal", 1),
    ("starts", "starts with", 1),
    ("ends", "ends with", 1),
    ("gt", "greater than", 1),
    ("ge", "greater than or equal", 1),
    ("lt", "less than", 1),
    ("le", "less than or equal", 1),
    ("between", "between (numbers)", 2),
    ("regex", "matches regular expression", 1),
    ("empty", "is empty text", 0),
    ("not_empty", "is not empty text", 0),
    ("null", "is NULL", 0),
    ("not_null", "is not NULL", 0),
]


def build_filter_text(op: str, value: str = "", value2: str = "") -> str:
    """Filter-row text for an operator chosen in the filter builder ('' clears the filter)."""
    if op == "contains":
        return value
    prefix = {"equals": "=", "not_equals": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}.get(op)
    if prefix is not None:
        return prefix + value if value else ""
    if op == "starts":
        return value + "%" if value else ""
    if op == "ends":
        return "%" + value if value else ""
    if op == "between":
        return f"{value}~{value2}" if value and value2 else ""
    if op == "regex":
        return f"/{value}/" if value else ""
    return {"empty": "=''", "not_empty": "<>''", "null": "=NULL", "not_null": "<>NULL"}.get(op, "")


def parse_filter_text(text: str) -> tuple[str, str, str]:
    """Best-effort inverse of ``build_filter_text`` (pre-fills the builder): (op, value, value2)."""
    if not text:
        return "contains", "", ""
    special = {"=''": "empty", "<>''": "not_empty", "=NULL": "null", "<>NULL": "not_null"}
    if text in special:
        return special[text], "", ""
    if "~" in text:
        a, b = text.split("~", 1)
        if _is_number(a) and _is_number(b):
            return "between", a, b
    if len(text) > 2 and text[0] == "/" and text[-1] == "/":
        return "regex", text[1:-1], ""
    for prefix, op in ((">=", "ge"), ("<=", "le"), ("<>", "not_equals"), (">", "gt"), ("<", "lt"), ("=", "equals")):
        if text.startswith(prefix):
            return op, text[len(prefix):], ""
    if text.endswith("%") and text.count("%") == 1:
        return "starts", text[:-1], ""
    if text.startswith("%") and text.count("%") == 1:
        return "ends", text[1:], ""
    return "contains", text, ""


def describe_filter(text: str) -> str:
    """Human readable form of a filter-row text, e.g. '>5' -> '> 5', 'abc' -> 'contains “abc”'."""
    op, v, v2 = parse_filter_text(text)
    if op == "contains" and "%" in v:
        return f"like “{v}”"
    return {
        "contains": f"contains “{v}”", "equals": f"= {v}", "not_equals": f"≠ {v}", "starts": f"starts with “{v}”",
        "ends": f"ends with “{v}”", "gt": f"> {v}", "ge": f"≥ {v}", "lt": f"< {v}", "le": f"≤ {v}",
        "between": f"between {v} and {v2}", "regex": f"matches /{v}/", "empty": "is empty", "not_empty": "is not empty",
        "null": "is NULL", "not_null": "is not NULL",
    }[op]
