"""Schema explorer sidebar (replaces upstream's "Database Structure" tab and "DB Schema" dock)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QMimeData, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QMenu, QTreeView, QVBoxLayout, QWidget

from ..core import sqlb
from ..core.db import DBBrowserDB
from .icons import icon
from .widgets import SearchLineEdit, label, tool_button

KindRole = Qt.UserRole + 10  # "table" | "view" | "index" | "trigger" | "column" | "group" | "schema"
IdentRole = Qt.UserRole + 11  # ObjectIdentifier of the object (or of the column's table)
NameRole = Qt.UserRole + 12  # object / column name
SqlRole = Qt.UserRole + 13


class _Filter(QSortFilterProxyModel):
    text = ""

    def set_text(self, text: str) -> None:
        self.text = text.strip().lower()
        self.invalidate()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        pattern = self.text
        if not pattern:
            return True
        model = self.sourceModel()
        idx = model.index(row, 0, parent)
        kind = idx.data(KindRole)
        if kind in ("group", "schema"):
            # keep groups that have matching children
            for i in range(model.rowCount(idx)):
                if self.filterAcceptsRow(i, idx):
                    return True
            return False
        if kind == "column":
            return pattern.lower() in (idx.data(NameRole) or "").lower() or \
                pattern.lower() in (idx.parent().data(NameRole) or "").lower()
        if pattern.lower() in (idx.data(NameRole) or "").lower():
            return True
        for i in range(model.rowCount(idx)):
            if self.filterAcceptsRow(i, idx):
                return True
        return False


class SchemaModel(QStandardItemModel):
    def mimeTypes(self):
        return ["text/plain"]

    def mimeData(self, indexes):
        names = []
        for ix in indexes:
            if ix.column() == 0 and ix.data(KindRole) not in ("group", "schema"):
                names.append(sqlb.escape_identifier(ix.data(NameRole)))
        md = QMimeData()
        md.setText(", ".join(names))
        return md


class SchemaExplorer(QWidget):
    openRequested = Signal(object)  # ObjectIdentifier (table/view)
    structureRequested = Signal(object)
    modifyRequested = Signal(object)
    dropRequested = Signal(str, object)  # kind, ObjectIdentifier
    createTableRequested = Signal()
    createIndexRequested = Signal(object)
    exportRequested = Signal(object)
    insertSqlRequested = Signal(str)
    copySqlRequested = Signal(str)
    schemaRequested = Signal(object)  # ObjectIdentifier, or None for the whole database

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db: Optional[DBBrowserDB] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 0)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(2)
        self.search = SearchLineEdit("Filter objects…")
        head.addWidget(self.search, 1)
        head.addWidget(tool_button("file-code", "Show database schema", on_click=lambda: self.schemaRequested.emit(None)))
        head.addWidget(tool_button("table-plus", "Create table…", on_click=self.createTableRequested.emit))
        head.addWidget(tool_button("refresh", "Refresh schema", on_click=self._refresh))
        lay.addLayout(head)
        self.model = SchemaModel(self)
        self.proxy = _Filter(self)
        self.proxy.setSourceModel(self.model)
        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(14)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragOnly)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.doubleClicked.connect(self._activated)
        self.tree.setFrameShape(QTreeView.NoFrame)
        lay.addWidget(self.tree, 1)
        self.summary = label("", "faint")
        self.summary.setContentsMargins(4, 2, 4, 6)
        lay.addWidget(self.summary)
        self.search.textChanged.connect(self._filter)
        self._expanded: set[str] = {"Tables", "main", "main/Tables"}

    # ------------------------------------------------------------------ data
    def set_db(self, db: DBBrowserDB) -> None:
        self.db = db
        db.structureUpdated.connect(self.populate)

    def _refresh(self) -> None:
        if self.db is not None and self.db.is_open():
            self.db.update_schema()

    def _remember_expanded(self) -> None:
        keys = set()

        def walk(parent: QModelIndex, path: str) -> None:
            for r in range(self.proxy.rowCount(parent)):
                ix = self.proxy.index(r, 0, parent)
                key = f"{path}/{ix.data(Qt.DisplayRole).split('  ')[0]}"
                if self.tree.isExpanded(ix):
                    keys.add(key)
                    walk(ix, key)

        walk(QModelIndex(), "")
        if keys:
            self._expanded = {k.lstrip("/") for k in keys}

    def populate(self) -> None:
        self._remember_expanded()
        self.model.clear()
        db = self.db
        if db is None or not db.is_open():
            self.summary.setText("")
            return
        multi = len([s for s, m in db.schemata.items() if s != "temp" or not m.empty()]) > 1
        root = self.model.invisibleRootItem()
        n_tables = n_views = 0
        for schema, objmap in db.schemata.items():
            if schema == "temp" and objmap.empty():
                continue
            if schema == "temp" and all(n.startswith("sqlite_") for n in objmap.tables) and not objmap.indices and not objmap.triggers:
                continue
            parent = root
            if multi:
                s_item = self._item(schema, "database", "schema", None, schema)
                s_item.setToolTip(f"Schema '{schema}'")
                root.appendRow(s_item)
                parent = s_item
            tables = {k: v for k, v in objmap.tables.items() if not v.is_view() and not k.startswith("sqlite_")}
            system = {k: v for k, v in objmap.tables.items() if k.startswith("sqlite_")}
            views = {k: v for k, v in objmap.tables.items() if v.is_view()}
            n_tables += len(tables)
            n_views += len(views)
            for title, icon_name, objs, kind in (("Tables", "table", tables, "table"), ("Views", "view", views, "view")):
                g = self._group(title, len(objs))
                for name in sorted(objs, key=str.lower):
                    t = objs[name]
                    it = self._item(name, icon_name if not t.is_virtual() else "sparkle", kind,
                                    sqlb.ObjectIdentifier(schema, name), name, t.original_sql)
                    pk = set(t.primary_key_names())
                    for f in t.fields:
                        fk = t.foreign_key([f.name])
                        col_icon = "key" if f.name in pk else ("link" if fk else "column")
                        ci = self._item(f.name, col_icon, "column", sqlb.ObjectIdentifier(schema, name), f.name)
                        ci.setData(f"{f.name}  {f.type}", Qt.DisplayRole)
                        tip = f"{f.name} {f.type}".strip()
                        if f.notnull:
                            tip += " NOT NULL"
                        if fk:
                            tip += f"\n→ {fk.to_string()}"
                        ci.setToolTip(tip)
                        it.appendRow(ci)
                    tip = t.original_sql if len(t.original_sql) < 1500 else t.original_sql[:1500] + "…"
                    it.setToolTip(tip)
                    g.appendRow(it)
                if objs or kind == "table":
                    parent.appendRow(g)
            for title, icon_name, objs, kind in (("Indices", "index", objmap.indices, "index"),
                                                 ("Triggers", "trigger", objmap.triggers, "trigger")):
                if not objs:
                    continue
                g = self._group(title, len(objs))
                for name in sorted(objs, key=str.lower):
                    o = objs[name]
                    it = self._item(name, icon_name, kind, sqlb.ObjectIdentifier(schema, name), name, o.original_sql)
                    it.setToolTip(f"on {o.base_table()}\n\n{o.original_sql}" if o.original_sql else f"on {o.base_table()} (automatic)")
                    it.setData(f"{name}  ·  {o.base_table()}", Qt.DisplayRole)
                    g.appendRow(it)
                parent.appendRow(g)
            if system:
                g = self._group("System", len(system))
                for name in sorted(system):
                    g.appendRow(self._item(name, "lock", "table", sqlb.ObjectIdentifier(schema, name), name,
                                           system[name].original_sql))
                parent.appendRow(g)
        self.summary.setText(f"{n_tables} table{'s' if n_tables != 1 else ''} · {n_views} view{'s' if n_views != 1 else ''}")
        self._restore_expanded()

    def _restore_expanded(self) -> None:
        def walk(parent: QModelIndex, path: str) -> None:
            for r in range(self.proxy.rowCount(parent)):
                ix = self.proxy.index(r, 0, parent)
                key = f"{path}/{ix.data(Qt.DisplayRole).split('  ')[0]}".lstrip("/")
                if key in self._expanded or (ix.data(KindRole) == "schema" and ix.data(NameRole) == "main"):
                    self.tree.expand(ix)
                    walk(ix, key)

        walk(QModelIndex(), "")
        if self.search.text():
            self.tree.expandToDepth(1)

    def _group(self, title: str, count: int) -> QStandardItem:
        it = QStandardItem(f"{title}  {count}")
        it.setData("group", KindRole)
        it.setData(title, NameRole)
        it.setEditable(False)
        it.setDragEnabled(False)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        return it

    def _item(self, text: str, icon_name: str, kind: str, ident, name: str, sql: str = "") -> QStandardItem:
        it = QStandardItem(icon(icon_name, "accent" if kind in ("table", "view") else
                                ("warning" if icon_name == "key" else None)), text)
        it.setData(kind, KindRole)
        it.setData(ident, IdentRole)
        it.setData(name, NameRole)
        it.setData(sql, SqlRole)
        it.setEditable(False)
        return it

    def _filter(self, text: str) -> None:
        self.proxy.set_text(text)
        if text:
            self.tree.expandToDepth(1)

    # ------------------------------------------------------------------ interaction
    def _activated(self, ix: QModelIndex) -> None:
        kind = ix.data(KindRole)
        if kind in ("table", "view"):
            self.openRequested.emit(ix.data(IdentRole))
        elif kind == "column":
            self.openRequested.emit(ix.data(IdentRole))
        elif kind in ("index", "trigger"):
            sql = ix.data(SqlRole)
            if sql:
                self.copySqlRequested.emit(sql)
        else:
            self.tree.setExpanded(ix, not self.tree.isExpanded(ix))

    def current_object(self) -> tuple[Optional[str], Optional[sqlb.ObjectIdentifier]]:
        ix = self.tree.currentIndex()
        if not ix.isValid():
            return None, None
        return ix.data(KindRole), ix.data(IdentRole)

    def _context_menu(self, pos) -> None:
        ix = self.tree.indexAt(pos)
        menu = QMenu(self)
        kind = ix.data(KindRole) if ix.isValid() else None
        ident = ix.data(IdentRole) if ix.isValid() else None
        name = ix.data(NameRole) if ix.isValid() else ""
        ro = self.db is None or self.db.read_only
        if kind in ("table", "view"):
            menu.addAction(icon("table"), "Browse Data", lambda: self.openRequested.emit(ident))
            menu.addAction(icon("column"), "Show Structure", lambda: self.structureRequested.emit(ident))
            menu.addAction(icon("file-code"), "Show Schema", lambda: self.schemaRequested.emit(ident))
            if kind == "table" and not name.startswith("sqlite_"):
                a = menu.addAction(icon("table-edit"), "Modify Table…", lambda: self.modifyRequested.emit(ident))
                a.setEnabled(not ro)
                a = menu.addAction(icon("index"), "Create Index…", lambda: self.createIndexRequested.emit(ident))
                a.setEnabled(not ro)
            menu.addAction(icon("download"), "Export…", lambda: self.exportRequested.emit(ident))
            menu.addSeparator()
            menu.addAction(icon("terminal"), "New SELECT in SQL editor",
                           lambda: self.insertSqlRequested.emit(f"SELECT * FROM {ident.to_string(True)};"))
        if kind == "column":
            menu.addAction(icon("table"), "Browse Table", lambda: self.openRequested.emit(ident))
            menu.addAction(icon("copy"), "Copy Column Name", lambda: self._copy(sqlb.escape_identifier(name)))
        if kind in ("table", "view", "index", "trigger"):
            sql = ix.data(SqlRole)
            menu.addAction(icon("copy"), "Copy Name", lambda: self._copy(name))
            a = menu.addAction(icon("file-code"), "Copy CREATE Statement", lambda: self._copy((sql or "").rstrip(";") + ";"))
            a.setEnabled(bool(sql))
            if not name.startswith("sqlite_"):
                menu.addSeparator()
                a = menu.addAction(icon("trash", "danger"), f"Delete {kind.title()}…", lambda: self.dropRequested.emit(kind, ident))
                a.setEnabled(not ro)
        if kind is None or kind in ("group", "schema"):
            a = menu.addAction(icon("table-plus"), "Create Table…", self.createTableRequested.emit)
            a.setEnabled(self.db is not None and self.db.is_open() and not ro)
            a = menu.addAction(icon("index"), "Create Index…", lambda: self.createIndexRequested.emit(None))
            a.setEnabled(self.db is not None and self.db.is_open() and not ro)
            a = menu.addAction(icon("file-code"), "Show Database Schema", lambda: self.schemaRequested.emit(None))
            a.setEnabled(self.db is not None and self.db.is_open())
            menu.addAction(icon("refresh"), "Refresh", self._refresh)
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy(self, text: str) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(text)
