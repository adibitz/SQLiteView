"""Schema viewer: the CREATE statements and a column overview for the whole database or one table."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QMessageBox,
                               QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core import sqlb
from ..core.db import DBBrowserDB
from .icons import icon
from .sql_editor import EditorWithFind, SqlEditor
from .widgets import SegmentedControl, label, tool_button, vsep


def _statement(sql: str) -> str:
    return sql.strip().rstrip(";") + ";"


def schema_sql(db: DBBrowserDB, ident: Optional[sqlb.ObjectIdentifier], include_related: bool = True) -> str:
    """CREATE statements for every object of the database, or for one table/view and its indices/triggers."""
    parts: list[str] = []
    for schema, objmap in db.schemata.items():
        if ident is not None and schema != ident.schema:
            continue
        prefix = "" if schema == "main" else f"{schema}."
        tables = [(n, t) for n, t in sorted(objmap.tables.items(), key=lambda kv: kv[0].lower())
                  if not n.startswith("sqlite_") and t.original_sql]
        if ident is not None:
            tables = [(n, t) for n, t in tables if n == ident.name]
        names = {n for n, _ in tables}
        for kind, wanted in (("Table", False), ("View", True)):
            for name, t in tables:
                if t.is_view() == wanted:
                    parts.append(f"-- {kind}: {prefix}{name}\n{_statement(t.original_sql)}")
        if not include_related:
            continue
        for kind, objs in (("Index", objmap.indices), ("Trigger", objmap.triggers)):
            for name, o in sorted(objs.items(), key=lambda kv: kv[0].lower()):
                if not o.original_sql:
                    continue  # automatic indices (UNIQUE / PRIMARY KEY) have no SQL
                if ident is not None and o.base_table() not in names:
                    continue
                parts.append(f"-- {kind}: {prefix}{name}  (on {o.base_table()})\n{_statement(o.original_sql)}")
    return "\n\n".join(parts) + ("\n" if parts else "")


class SchemaTab(QWidget):
    """Document tab with the database schema as SQL and as a column tree."""

    kind = "schema"
    statusMessage = Signal(str)
    openSqlRequested = Signal(str)
    openTableRequested = Signal(object)
    titleChanged = Signal()

    def __init__(self, db: DBBrowserDB, parent=None):
        super().__init__(parent)
        self.db = db
        self.ident: Optional[sqlb.ObjectIdentifier] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setProperty("role", "toolbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(6)
        title_icon = label()
        title_icon.setPixmap(icon("file-code", "accent").pixmap(18, 18))
        bl.addWidget(title_icon)
        bl.addWidget(label("Schema", "h2"))
        bl.addSpacing(8)
        self.scope = QComboBox()
        self.scope.setMinimumWidth(200)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        bl.addWidget(self.scope)
        self.related = QCheckBox("Indices && triggers")
        self.related.setChecked(True)
        self.related.toggled.connect(self.reload)
        bl.addWidget(self.related)
        bl.addSpacing(8)
        self.view_switch = SegmentedControl([("SQL", "file-code"), ("Columns", "column")])
        self.view_switch.changed.connect(lambda i: self.stack.setCurrentIndex(i))
        bl.addWidget(self.view_switch)
        bl.addStretch(1)
        self.summary = label("", "faint")
        bl.addWidget(self.summary)
        bl.addWidget(vsep())
        bl.addWidget(tool_button("copy", "Copy schema SQL", on_click=self.copy_sql))
        bl.addWidget(tool_button("save", "Save schema as .sql…", on_click=self.save_sql))
        bl.addWidget(tool_button("terminal", "Open in SQL editor", on_click=lambda: self.openSqlRequested.emit(self.sql())))
        bl.addWidget(tool_button("refresh", "Refresh", on_click=self.reload))
        lay.addWidget(bar)

        self.stack = QStackedWidget()
        self.editor = SqlEditor(read_only=True)
        self.stack.addWidget(EditorWithFind(self.editor))
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["Name", "Type", "Key", "Not null", "Default", "References / details"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(self._tree_activated)
        self.stack.addWidget(self.tree)
        lay.addWidget(self.stack, 1)

    # ------------------------------------------------------------------ data
    def title_text(self) -> str:
        return f"Schema — {self.ident.to_display_string()}" if self.ident else "Schema"

    def set_scope(self, ident: Optional[sqlb.ObjectIdentifier]) -> None:
        self.ident = ident
        self._fill_scope()
        self.reload()

    def _fill_scope(self) -> None:
        self.scope.blockSignals(True)
        self.scope.clear()
        self.scope.addItem(icon("database", "accent"), "All tables", "")
        for schema, objmap in self.db.schemata.items():
            for name in sorted(objmap.tables, key=str.lower):
                t = objmap.tables[name]
                if name.startswith("sqlite_"):
                    continue
                oid = sqlb.ObjectIdentifier(schema, name)
                self.scope.addItem(icon("view" if t.is_view() else "table", "accent"), oid.to_display_string(),
                                   oid.to_serialised())
        i = self.scope.findData(self.ident.to_serialised()) if self.ident is not None else 0
        if i < 0:
            self.ident = None
            i = 0
        self.scope.setCurrentIndex(i)
        self.scope.blockSignals(False)

    def _scope_changed(self, _i: int) -> None:
        data = self.scope.currentData()
        self.ident = sqlb.ObjectIdentifier.from_serialised(data) if data else None
        self.reload()
        self.titleChanged.emit()

    def sql(self) -> str:
        return schema_sql(self.db, self.ident, self.related.isChecked())

    def reload(self) -> None:
        if not self.db.is_open():
            return
        if self.ident is not None and self.db.get_table_by_name(self.ident) is None:
            self.ident = None
        if self.scope.count() == 0 or self.scope.count() - 1 != sum(
                1 for m in self.db.schemata.values() for n in m.tables if not n.startswith("sqlite_")):
            self._fill_scope()
        self.editor.setPlainText(self.sql())
        self._fill_tree()

    def _fill_tree(self) -> None:
        self.tree.clear()
        n_tables = n_cols = 0
        for schema, objmap in self.db.schemata.items():
            for name in sorted(objmap.tables, key=str.lower):
                t = objmap.tables[name]
                if name.startswith("sqlite_"):
                    continue
                oid = sqlb.ObjectIdentifier(schema, name)
                if self.ident is not None and oid != self.ident:
                    continue
                n_tables += 1
                kind = "VIEW" if t.is_view() else ("VIRTUAL" if t.is_virtual() else "TABLE")
                top = QTreeWidgetItem([oid.to_display_string(), kind, "", "", "", f"{len(t.fields)} columns"])
                top.setIcon(0, icon("view" if t.is_view() else "table", "accent"))
                f = top.font(0)
                f.setBold(True)
                top.setFont(0, f)
                top.setData(0, Qt.UserRole, oid)
                pk = t.primary_key_names()
                for fld in t.fields:
                    n_cols += 1
                    fk = t.foreign_key([fld.name])
                    details = fk.to_string() if fk else ""
                    if fld.generated:
                        details = f"GENERATED {fld.generated.storage}: {fld.generated.expression}"
                    extra = [x for x in (("UNIQUE" if fld.unique else ""), (f"CHECK({fld.check})" if fld.check else ""),
                                         (f"COLLATE {fld.collation}" if fld.collation else "")) if x]
                    if extra:
                        details = "  ".join([details] + extra).strip()
                    key = "PK" if fld.name in pk else ("FK" if fk else "")
                    it = QTreeWidgetItem([fld.name, fld.type, key, "✓" if fld.notnull else "", fld.default_value, details])
                    it.setIcon(0, icon("key", "warning") if key == "PK" else icon("link", "accent") if key == "FK" else icon("column"))
                    it.setData(0, Qt.UserRole, oid)
                    top.addChild(it)
                self.tree.addTopLevelItem(top)
                top.setExpanded(True)
        for c, w in ((0, 220), (1, 110), (2, 50), (3, 70), (4, 110)):
            self.tree.setColumnWidth(c, w)
        self.summary.setText(f"{n_tables} object{'s' if n_tables != 1 else ''} · {n_cols} columns")

    def _tree_activated(self, item: QTreeWidgetItem, _col: int) -> None:
        oid = item.data(0, Qt.UserRole)
        if oid is not None:
            self.openTableRequested.emit(oid)

    # ------------------------------------------------------------------ actions
    def copy_sql(self) -> None:
        QGuiApplication.clipboard().setText(self.sql())
        self.statusMessage.emit("Schema copied to clipboard")

    def save_sql(self) -> None:
        name = self.ident.name if self.ident else "schema"
        path, _ = QFileDialog.getSaveFileName(self, "Save schema", f"{name}.sql", "SQL files (*.sql);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.sql())
        except OSError as e:
            QMessageBox.warning(self, "Save schema", f"Couldn't write file:\n{e}")
            return
        self.statusMessage.emit("Schema saved")
