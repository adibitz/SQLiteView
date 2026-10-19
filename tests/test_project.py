"""Project files (.sqbpro) and editable views through a pseudo primary key."""
from __future__ import annotations

import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from sqliteview.core import project as sqbpro  # noqa: E402
from sqliteview.core import sqlb  # noqa: E402
from sqliteview.core.db import DBBrowserDB  # noqa: E402
from sqliteview.core.query import SortDirection  # noqa: E402
from test_ui import demo_db, loaded, pump, qapp, window  # noqa: E402,F401 - shared fixtures and helpers

CUSTOMERS = sqlb.ObjectIdentifier("main", "customers")

# What DB Browser for SQLite 3.13 writes (shortened): no whitespace between elements
UPSTREAM_PROJECT = (
    '<?xml version="1.0" encoding="UTF-8"?><sqlb_project>'
    '<db path="demo.db" readonly="1" foreign_keys="1" case_sensitive_like="0" temp_store="0" wal_autocheckpoint="1000" '
    'synchronous="2"/>'
    '<attached><db schema="extra" path="C:/data/extra.db"/></attached>'
    '<window><main_tabs open="structure browser pragmas query" current="1"/></window>'
    '<tab_structure><column_width id="0" width="300"/><expanded_item id="0" parent="-1"/></tab_structure>'
    '<tab_browse><table title="customers" custom_title="0" dock_id="1" table="4,9:maincustomers"/>'
    '<table title="order_summary" custom_title="0" dock_id="2" table="4,13:mainorder_summary"/>'
    '<dock_state state="000000ff"/><default_encoding codec=""/>'
    '<browse_table_settings>'
    '<table schema="main" name="customers" show_row_id="0" encoding="" plot_x_axis="" unlock_view_pk="_rowid_" '
    'freeze_columns="0"><sort><column index="1" mode="1"/></sort>'
    '<column_widths><column index="2" value="180"/></column_widths>'
    '<filter_values><column index="3" value="=IN"/></filter_values>'
    '<conditional_formats/><row_id_formats/><display_formats/><hidden_columns/><plot_y_axes/>'
    '<global_filter><filter value="Sharma"/></global_filter></table>'
    '<table schema="main" name="order_summary" show_row_id="0" encoding="" plot_x_axis="" unlock_view_pk="customer" '
    'freeze_columns="0"><sort/><column_widths/><filter_values/><conditional_formats/><row_id_formats/>'
    '<display_formats/><hidden_columns/><plot_y_axes/><global_filter/></table>'
    '</browse_table_settings></tab_browse>'
    '<tab_sql><sql name="Report">SELECT 1;\nSELECT 2;</sql>'
    '<sql name="linked.sql" filename="C:/q/linked.sql">-- Reference to file "C:/q/linked.sql" (not supported by this '
    'version) --</sql><current_tab id="1"/></tab_sql>'
    '</sqlb_project>')


# ---------------------------------------------------------------------------- file format

def test_reads_upstream_project():
    p = sqbpro.project_from_xml(UPSTREAM_PROJECT)
    assert p.db_path == "demo.db" and p.read_only
    assert p.pragmas == {"foreign_keys": "1", "case_sensitive_like": "0", "temp_store": "0",
                         "wal_autocheckpoint": "1000", "synchronous": "2"}
    assert p.attached == [("extra", "C:/data/extra.db")]
    assert p.tables == [CUSTOMERS, sqlb.ObjectIdentifier("main", "order_summary")]
    s = p.table_settings[CUSTOMERS]
    assert s.sort == [(1, 1)] and s.filters == {3: "=IN"} and s.column_widths == {2: 180}
    assert s.global_filters == ["Sharma"] and s.unlock_view_pk == "_rowid_"
    assert p.table_settings[sqlb.ObjectIdentifier("main", "order_summary")].unlock_view_pk == "customer"
    assert [(b.name, b.text, b.filename) for b in p.sql_tabs] == [
        ("Report", "SELECT 1;\nSELECT 2;", ""), ("linked.sql", "", "C:/q/linked.sql")]
    assert p.current_sql_tab == 1
    assert p.documents == [] and p.current_document == -1


def test_reads_old_current_table_element():
    p = sqbpro.project_from_xml('<sqlb_project><db path="a.db"/><tab_browse><current_table name="main.t1"/>'
                                '</tab_browse></sqlb_project>')
    assert p.tables == [sqlb.ObjectIdentifier("main", "t1")]


def test_roundtrip_and_upstream_friendly_output(tmp_path):
    p = sqbpro.Project(db_path=str(tmp_path / "demo.db"), read_only=True, pragmas={"foreign_keys": "1"},
                       attached=[("extra", "C:/x & y/extra.db")])
    view = sqlb.ObjectIdentifier("aux", 'we"ird, view')
    p.tables = [CUSTOMERS, view]
    p.table_settings = {
        CUSTOMERS: sqbpro.TableSettings(sort=[(1, 1), (0, 0)], filters={2: "<>NULL", 3: "a<b"},
                                        global_filters=["x", "/a b/"], column_widths={1: 50, 2: 120}),
        view: sqbpro.TableSettings(unlock_view_pk="id"),
    }
    p.sql_tabs = [sqbpro.SqlBuffer("Q <1>", "SELECT '&';\n  -- tab\tend\n"), sqbpro.SqlBuffer("f.sql", filename="C:/f.sql")]
    p.current_sql_tab = 1
    p.documents = [sqbpro.DocumentRef("sql", 1), sqbpro.DocumentRef("table", 0), sqbpro.DocumentRef("info"),
                   sqbpro.DocumentRef("table", 1), sqbpro.DocumentRef("sql", 0)]
    p.current_document = 3
    p.geometry, p.window_state = b"\x01\x02geo", b"\x00state\xff"

    path = str(tmp_path / "demo.sqbpro")
    sqbpro.save_project(path, p)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # Upstream's stream reader needs: declaration directly followed by the root, no whitespace between elements,
    # and <main_tabs> first inside <window>
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?><sqlb_project><db path="demo.db" readonly="1"')
    assert "><window><main_tabs " in text and "> <" not in text and ">\n<" not in text
    assert '<main_tabs open="structure browser pragmas query" current="1"' in text  # current document is a table
    assert not os.path.exists(path + ".tmp")

    back = sqbpro.load_project(path)
    assert back.db_path == "demo.db"  # stored relative to the project, like upstream
    assert sqbpro.resolve_db_path(back.db_path, path) == os.path.join(str(tmp_path), "demo.db")
    for name in ("read_only", "pragmas", "attached", "tables", "table_settings", "sql_tabs", "current_sql_tab",
                 "documents", "current_document", "geometry", "window_state"):
        expected = getattr(p, name)
        if name == "sql_tabs":
            expected = [sqbpro.SqlBuffer("Q <1>", "SELECT '&';\n  -- tab\tend\n"), sqbpro.SqlBuffer("f.sql", "", "C:/f.sql")]
        assert getattr(back, name) == expected, name


def test_db_in_other_folder_keeps_absolute_path(tmp_path):
    other = tmp_path / "data"
    other.mkdir()
    p = sqbpro.Project(db_path=str(other / "x.db"))
    back = sqbpro.project_from_xml(sqbpro.project_to_xml(p, str(tmp_path / "x.sqbpro")))
    assert back.db_path == str(other / "x.db")


@pytest.mark.parametrize("text", ["", "<not-closed", "<?xml version='1.0'?><something/>"])
def test_rejects_non_project_files(text):
    with pytest.raises(sqbpro.ProjectError):
        sqbpro.project_from_xml(text)


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(sqbpro.ProjectError):
        sqbpro.load_project(str(tmp_path / "nope.sqbpro"))


def test_project_file_name():
    assert sqbpro.project_file_name("a/b") == "a/b.sqbpro"
    assert sqbpro.project_file_name("a/b.SQBPRO") == "a/b.SQBPRO"


def test_attached_databases(tmp_path):
    main, extra = str(tmp_path / "m.db"), str(tmp_path / "e.db")
    sqlite3.connect(extra).execute("CREATE TABLE t(x)").connection.close()
    db = DBBrowserDB()
    assert db.attached_databases() == []
    assert db.create(main)
    assert db.attached_databases() == []
    assert db.attach(extra, "extra")
    assert [(s, os.path.normcase(f)) for s, f in db.attached_databases()] == [("extra", os.path.normcase(extra))]
    db.close(force=True)


# ---------------------------------------------------------------------------- editable views

def _make_editable_view(db) -> sqlb.ObjectIdentifier:
    assert db.execute_multi_sql(
        "CREATE VIEW v_cust AS SELECT id, name, country FROM customers;"
        "CREATE TRIGGER v_cust_upd INSTEAD OF UPDATE ON v_cust BEGIN "
        "  UPDATE customers SET name = NEW.name, country = NEW.country WHERE id = OLD.id; END;"
        "CREATE TRIGGER v_cust_del INSTEAD OF DELETE ON v_cust BEGIN DELETE FROM customers WHERE id = OLD.id; END;")
    db.update_schema()
    return sqlb.ObjectIdentifier("main", "v_cust")


def test_view_pseudo_primary_key_enables_editing(window, qapp):
    ident = _make_editable_view(window.db)
    tab = window.open_table(ident)
    assert pump(qapp, loaded(tab.model))
    assert tab.btn_pk.isVisibleTo(tab) and tab.btn_pk.isEnabled()
    assert not tab.model.is_editable() and not tab.btn_del.isEnabled() and tab.pseudo_pk() is None

    tab._fill_pk_menu()
    choices = [a.text() for a in tab.pk_menu.actions() if a.isCheckable()]
    assert choices == ["None (read-only)", "id", "name", "country"]
    next(a for a in tab.pk_menu.actions() if a.text() == "id").trigger()
    assert tab.pseudo_pk() == "id" and tab.btn_pk.text() == "id"
    assert pump(qapp, loaded(tab.model))
    assert tab.model.is_editable() and tab.btn_del.isEnabled()

    row_id = tab.model.raw_value(0, 0)
    assert row_id == tab.model.raw_value(0, 1)  # the pseudo key is the row identifier
    assert tab.model.setData(tab.model.index(0, 2), "Via view")
    assert window.db.query_single_value(f"SELECT name FROM customers WHERE id = {row_id}") == "Via view"
    count = window.db.query_single_value("SELECT COUNT(*) FROM customers")
    assert tab.model.remove_row_list([0])
    assert window.db.query_single_value("SELECT COUNT(*) FROM customers") == count - 1

    tab.reload()  # the choice survives a refresh
    assert tab.pseudo_pk() == "id" and tab.model.is_editable()
    tab.set_pseudo_pk(None)
    assert tab.pseudo_pk() is None and not tab.model.is_editable() and not tab.btn_pk.text()


def test_pseudo_pk_button_only_for_views(window, qapp):
    tab = window.open_table(CUSTOMERS)
    assert not tab.btn_pk.isVisibleTo(tab)
    tab.set_pseudo_pk("name")  # ignored for tables
    assert tab.model.query.rowid_columns == ["_rowid_"]


def test_pseudo_pk_dropped_from_view_is_reset(window, qapp):
    ident = _make_editable_view(window.db)
    tab = window.open_table(ident)
    tab.set_pseudo_pk("country")
    assert tab.pseudo_pk() == "country"
    assert window.db.execute_multi_sql("DROP VIEW v_cust; CREATE VIEW v_cust AS SELECT id, name FROM customers;")
    window.db.update_schema()
    tab.reload()
    assert tab.pseudo_pk() is None and not tab.model.is_editable()


# ---------------------------------------------------------------------------- save / restore in the app

def test_save_and_open_project_restores_workspace(window, qapp, tmp_path, monkeypatch):
    from sqliteview.ui.main_window import MainWindow
    from sqliteview.ui.sql_tab import SqlTab
    from sqliteview.ui.table_tab import TableTab

    extra = str(tmp_path / "extra.db")
    sqlite3.connect(extra).execute("CREATE TABLE notes(x)").connection.close()
    assert window.db.attach(extra, "extra")
    view = _make_editable_view(window.db)
    window.write_changes()  # the view must exist on disk for the reopened project

    sql = window.open_sql_tab("SELECT 42;\n")
    sql.name = "Answers"
    cust = window.open_table(CUSTOMERS)
    assert pump(qapp, loaded(cust.model))
    cust.set_filter("country", "=IN")
    cust.model.update_filter(cust.model.column_index("country"), "=IN")
    cust.grid._sort_requested(cust.model.column_index("name"), False)
    cust.grid._sort_requested(cust.model.column_index("name"), False)  # second click: descending
    cust.search.setText("a")
    cust.grid.setColumnWidth(2, 222)
    vtab = window.open_table(view)
    vtab.set_pseudo_pk("id")
    window.tabs.setCurrentWidget(cust)
    expected_where = dict(cust.model.query.where)

    project = str(tmp_path / "work.sqbpro")
    assert window.save_project(project)
    assert window.project_path == os.path.abspath(project)
    assert os.path.normcase(window._recent_files()[0]) == os.path.normcase(os.path.abspath(project))

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w2 = MainWindow()
    try:
        assert w2.open_database(project)  # project files open through the normal open path too
        assert w2.project_path == os.path.abspath(project)
        assert os.path.normcase(w2.db.cur_file) == os.path.normcase(window.db.cur_file)
        assert "extra" in w2.db.schemata
        docs = [w2.tabs.widget(i) for i in range(w2.tabs.count())]
        kinds = [type(d).__name__ for d in docs]
        assert kinds == [type(window.tabs.widget(i)).__name__ for i in range(window.tabs.count())]
        assert isinstance(w2.tabs.currentWidget(), TableTab) and w2.tabs.currentWidget().ident == CUSTOMERS

        sql2 = next(d for d in docs if isinstance(d, SqlTab))
        assert sql2.name == "Answers" and sql2.editor.toPlainText() == "SELECT 42;\n"
        c2 = next(d for d in docs if isinstance(d, TableTab) and d.ident == CUSTOMERS)
        assert c2.model.query.where == expected_where
        assert c2.grid.filter_header.filter_text(c2.model.column_index("country")) == "=IN"
        assert [(o.expr, o.direction) for o in c2.model.query.order_by] == [("name", SortDirection.Descending)]
        assert c2.search.text() == "a" and c2.model.query.global_where
        assert c2.grid.columnWidth(2) == 222
        assert pump(qapp, loaded(c2.model))
        assert all(c2.model.raw_value(r, c2.model.column_index("country")) == "IN"
                   for r in range(min(5, c2.model.rowCount())))
        v2 = next(d for d in docs if isinstance(d, TableTab) and d.ident == view)
        assert v2.pseudo_pk() == "id" and v2.model.is_editable()
    finally:
        w2.close()
        pump(qapp, timeout=0.2)


def test_open_project_read_only_and_bad_files(qapp, demo_db, tmp_path, monkeypatch):
    from sqliteview.ui.main_window import MainWindow
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.Ok))
    project = tmp_path / "ro.sqbpro"
    project.write_text(sqbpro.project_to_xml(sqbpro.Project(db_path=demo_db, read_only=True), str(project)),
                       encoding="utf-8")
    bad = tmp_path / "bad.sqbpro"
    bad.write_text("<html/>", encoding="utf-8")
    w = MainWindow()
    try:
        assert not w.open_project(str(bad)) and warnings
        assert w.open_project(str(project))
        assert w.db.read_only
        assert w.tabs.count() == 1  # no tabs stored: the Database Info page, as for a plain database
    finally:
        w.close()
