"""Project files (``.sqbpro``). Port of upstream ``MainWindow::saveProject`` / ``loadProject``.

Only data and XML live here; the main window collects and restores the state. The XML layout follows upstream so
projects can be exchanged with DB Browser for SQLite: the database, its pragmas, attached databases, open table tabs
with their browse settings and SQL editors. This edition's document tab order and window layout are stored in extra
elements placed where upstream's reader ignores them.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .sqlb import ObjectIdentifier

PROJECT_EXTENSION = ".sqbpro"
PROJECT_FILTER = "DB Browser for SQLite project file (*.sqbpro)"

# Connection pragmas stored on the <db> element, in upstream's order
PROJECT_PRAGMAS = ("foreign_keys", "case_sensitive_like", "temp_store", "wal_autocheckpoint", "synchronous")

# Upstream's main tabs; written so upstream opens on the matching tab
_UPSTREAM_TABS = "structure browser pragmas query"
_UPSTREAM_TAB_INDEX = {"table": 1, "info": 2, "sql": 3}


class ProjectError(Exception):
    """The file couldn't be read or isn't a project file."""


@dataclass
class TableSettings:
    """Browse settings of one table or view (the parts of upstream ``BrowseDataTableSettings`` this edition has).

    Columns are field indices (0 = first column of the table), like in the XML; column widths use model columns
    (0 = rowid)."""
    sort: list[tuple[int, int]] = field(default_factory=list)  # (field index, 0 = ascending / 1 = descending)
    filters: dict[int, str] = field(default_factory=dict)  # field index -> filter text as typed
    global_filters: list[str] = field(default_factory=list)
    column_widths: dict[int, int] = field(default_factory=dict)
    unlock_view_pk: str = "_rowid_"  # pseudo primary key of a view; "_rowid_" means not editable


@dataclass
class SqlBuffer:
    """One SQL editor: either text kept in the project or a reference to a .sql file."""
    name: str
    text: str = ""
    filename: str = ""


@dataclass
class DocumentRef:
    """A document tab: ``kind`` is table/sql/info, ``index`` points into ``tables`` or ``sql_tabs``."""
    kind: str
    index: int = 0


@dataclass
class Project:
    db_path: str = ""
    read_only: bool = False
    pragmas: dict[str, str] = field(default_factory=dict)
    attached: list[tuple[str, str]] = field(default_factory=list)  # (schema, path)
    tables: list[ObjectIdentifier] = field(default_factory=list)  # open table tabs
    table_settings: dict[ObjectIdentifier, TableSettings] = field(default_factory=dict)
    sql_tabs: list[SqlBuffer] = field(default_factory=list)
    current_sql_tab: int = 0
    documents: list[DocumentRef] = field(default_factory=list)  # tab order; empty = tables, then SQL editors
    current_document: int = -1
    geometry: bytes = b""
    window_state: bytes = b""


# ---------------------------------------------------------------------------- paths

def project_file_name(path: str) -> str:
    """Append ``.sqbpro`` unless the name already ends with it (as upstream does)."""
    return path if is_project_file(path) else path + PROJECT_EXTENSION


def is_project_file(path: str) -> bool:
    return path.lower().endswith(PROJECT_EXTENSION)


def _stored_db_path(db_path: str, project_path: str) -> str:
    """Upstream stores just the file name when the database sits next to the project."""
    if db_path == ":memory:":
        return db_path
    same_dir = os.path.normcase(os.path.dirname(os.path.abspath(db_path))) == \
        os.path.normcase(os.path.dirname(os.path.abspath(project_path)))
    return os.path.basename(db_path) if same_dir else db_path


def resolve_db_path(db_path: str, project_path: str) -> str:
    """Upstream: a path that doesn't exist as written is taken relative to the project's folder."""
    if not db_path or db_path == ":memory:" or os.path.exists(db_path):
        return db_path
    return os.path.join(os.path.dirname(os.path.abspath(project_path)), db_path)


# ---------------------------------------------------------------------------- writing

def _sub(parent: ET.Element, tag: str, **attrs) -> ET.Element:
    return ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})


def _write_table_settings(el: ET.Element, s: TableSettings) -> None:
    el.set("show_row_id", "0")
    el.set("encoding", "")
    el.set("plot_x_axis", "")
    el.set("unlock_view_pk", s.unlock_view_pk)
    el.set("freeze_columns", "0")
    sort = _sub(el, "sort")
    for index, mode in s.sort:
        _sub(sort, "column", index=index, mode=mode)
    widths = _sub(el, "column_widths")
    for col, w in sorted(s.column_widths.items()):
        _sub(widths, "column", index=col, value=w)
    filters = _sub(el, "filter_values")
    for index, value in sorted(s.filters.items()):
        if value:
            _sub(filters, "column", index=index, value=value)
    for tag in ("conditional_formats", "row_id_formats", "display_formats", "hidden_columns", "plot_y_axes"):
        _sub(el, tag)
    glob = _sub(el, "global_filter")
    for value in s.global_filters:
        _sub(glob, "filter", value=value)


def project_to_xml(project: Project, project_path: str) -> str:
    """Serialise ``project`` as it will be stored at ``project_path`` (the database path may become relative)."""
    root = ET.Element("sqlb_project")
    db = _sub(root, "db", path=_stored_db_path(project.db_path, project_path), readonly=int(project.read_only))
    for p in PROJECT_PRAGMAS:
        if p in project.pragmas:
            db.set(p, str(project.pragmas[p]))
    attached = _sub(root, "attached")
    for schema, path in project.attached:
        _sub(attached, "db", schema=schema, path=path)

    window = _sub(root, "window")
    current_kind = (project.documents[project.current_document].kind
                    if 0 <= project.current_document < len(project.documents) else "")
    _sub(window, "main_tabs", open=_UPSTREAM_TABS, current=_UPSTREAM_TAB_INDEX.get(current_kind, 0))
    # This edition's extensions must come after <main_tabs>: upstream stops reading <window> at the first one
    docs = _sub(window, "documents", current=project.current_document)
    for d in project.documents:
        _sub(docs, "document", kind=d.kind, index=d.index)
    if project.geometry or project.window_state:
        _sub(window, "layout", geometry=project.geometry.hex(), state=project.window_state.hex())

    browse = _sub(root, "tab_browse")
    for i, ident in enumerate(project.tables):
        _sub(browse, "table", title=ident.to_display_string(), custom_title="0", dock_id=i + 1,
             table=ident.to_serialised())
    settings = _sub(browse, "browse_table_settings")
    for ident, s in project.table_settings.items():
        _write_table_settings(_sub(settings, "table", schema=ident.schema, name=ident.name), s)

    sql = _sub(root, "tab_sql")
    for buf in project.sql_tabs:
        el = _sub(sql, "sql", name=buf.name)
        if buf.filename:
            el.set("filename", buf.filename)
            # Same placeholder upstream writes for versions that don't know linked files
            el.text = f'-- Reference to file "{buf.filename}" (not supported by this version) --'
        else:
            el.text = buf.text
    _sub(sql, "current_tab", id=project.current_sql_tab)
    # No whitespace between elements: upstream's stream reader would see it as content and stop early
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")


def save_project(path: str, project: Project) -> None:
    """Write the project, replacing the file only once it's complete (upstream uses ``QSaveFile``).
    Raises ``OSError``."""
    data = project_to_xml(project, path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------- reading

def _int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _hex(value: Optional[str]) -> bytes:
    try:
        return bytes.fromhex(value or "")
    except ValueError:
        return b""


def _read_table_settings(el: ET.Element) -> TableSettings:
    s = TableSettings(unlock_view_pk=el.get("unlock_view_pk") or "_rowid_")
    for c in el.findall("sort/column"):
        if (i := _int(c.get("index"), -1)) >= 0:
            s.sort.append((i, 1 if _int(c.get("mode")) else 0))
    for c in el.findall("column_widths/column"):
        s.column_widths[_int(c.get("index"))] = _int(c.get("value"))
    for c in el.findall("filter_values/column"):
        if (i := _int(c.get("index"), -1)) >= 0 and c.get("value"):
            s.filters[i] = c.get("value")
    s.global_filters = [f.get("value") for f in el.findall("global_filter/filter") if f.get("value")]
    return s


def _old_table_name(name: str) -> ObjectIdentifier:
    """``<current_table name>`` from upstream 3.11 and older: serialised, or "schema.table" / "table"."""
    ident = ObjectIdentifier.from_serialised(name)
    if ident is not None:
        return ident
    schema, dot, rest = name.partition(".")
    return ObjectIdentifier(schema, rest) if dot else ObjectIdentifier("main", name)


def project_from_xml(text: str) -> Project:
    """Parse a project. Raises ``ProjectError`` if it isn't one."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ProjectError(f"Invalid project file: {e}") from e
    if root.tag != "sqlb_project":
        raise ProjectError("This is not a DB Browser for SQLite project file.")

    p = Project()
    db = root.find("db")
    if db is not None:
        p.db_path = db.get("path", "")
        p.read_only = bool(_int(db.get("readonly")))
        p.pragmas = {k: db.get(k) for k in PROJECT_PRAGMAS if db.get(k)}
    p.attached = [(a.get("schema"), a.get("path")) for a in root.findall("attached/db")
                  if a.get("schema") and a.get("path")]

    docs = root.find("window/documents")
    if docs is not None:
        p.current_document = _int(docs.get("current"), -1)
        p.documents = [DocumentRef(d.get("kind", ""), _int(d.get("index"))) for d in docs.findall("document")]
    layout = root.find("window/layout")
    if layout is not None:
        p.geometry = _hex(layout.get("geometry"))
        p.window_state = _hex(layout.get("state"))

    browse = root.find("tab_browse")
    if browse is not None:
        for t in browse.findall("table"):
            ident = ObjectIdentifier.from_serialised(t.get("table", ""))
            if ident is not None and not ident.is_empty():
                p.tables.append(ident)
        old = browse.find("current_table")
        if old is not None and not p.tables:
            ident = _old_table_name(old.get("name", ""))
            if not ident.is_empty():
                p.tables.append(ident)
        for t in browse.findall("browse_table_settings/table"):
            p.table_settings[ObjectIdentifier(t.get("schema", "main"), t.get("name", ""))] = _read_table_settings(t)

    sql = root.find("tab_sql")
    if sql is not None:
        for el in sql.findall("sql"):
            filename = el.get("filename", "")
            p.sql_tabs.append(SqlBuffer(el.get("name", ""), "" if filename else (el.text or ""), filename))
        cur = sql.find("current_tab")
        if cur is not None:
            p.current_sql_tab = _int(cur.get("id"))
    return p


def load_project(path: str) -> Project:
    """Read a project file. Raises ``ProjectError``."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise ProjectError(f"Could not read the project file.\nReason: {e}") from e
    return project_from_xml(text)
