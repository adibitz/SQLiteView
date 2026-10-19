"""UI behaviour tests (run offscreen): dialogs, grid clipboard, navigation, SQL tab."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sqliteview.core import sqlb  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    from sqliteview.ui.theme import theme
    theme.apply()
    return app


def loaded(model, row=0):
    return lambda: model.rowCount() > row and model.row_available(row)


def pump(app, cond=lambda: False, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def demo_db(tmp_path):
    path = tmp_path / "demo.db"
    subprocess.run([sys.executable, os.path.join(APP_DIR, "scripts", "make_demo_db.py"), str(path)], check=True,
                   capture_output=True)
    return str(path)


@pytest.fixture
def window(qapp, demo_db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.Ok))
    from sqliteview.ui.main_window import MainWindow
    w = MainWindow()
    w.show()
    assert w.open_database(demo_db)
    w.warnings = warnings
    yield w
    w.db.revert_all()
    w.close()
    pump(qapp, timeout=0.2)


def test_browse_edit_and_write(window, qapp):
    tab = window.open_table(sqlb.ObjectIdentifier("main", "customers"))
    assert pump(qapp, loaded(tab.model))
    assert tab.grid.isColumnHidden(0)  # rowid column
    assert tab.model.setData(tab.model.index(0, 2), "Renamed")
    assert window.db.dirty and window.act_write.isEnabled()
    window.write_changes()
    assert not window.db.dirty
    con = sqlite3.connect(window.db.cur_file)
    assert con.execute("SELECT name FROM customers WHERE id=1").fetchone()[0] == "Renamed"


def test_grid_copy_paste_and_null(window, qapp):
    tab = window.open_table(sqlb.ObjectIdentifier("main", "customers"))
    assert pump(qapp, loaded(tab.model, 3))
    tab.grid.select_cell(0, 2)
    tab.grid.copy()
    assert QGuiApplication.clipboard().text() == tab.model.raw_value(0, 2)
    tab.grid.select_cell(1, 2)
    tab.grid.paste()
    assert tab.model.raw_value(1, 2) == tab.model.raw_value(0, 2)
    tab.grid.select_cell(1, 4)
    tab.grid.set_selection_null()
    assert tab.model.raw_value(1, 4) is None
    QGuiApplication.clipboard().setText("a\tb\nc\td\n")
    tab.grid.select_cell(2, 2)
    tab.grid.paste()
    assert [tab.model.raw_value(2, 2), tab.model.raw_value(2, 3), tab.model.raw_value(3, 2), tab.model.raw_value(3, 3)] == \
        ["a", "b", "c", "d"]


def test_foreign_key_jump(window, qapp):
    tab = window.open_table(sqlb.ObjectIdentifier("main", "orders"))
    assert pump(qapp, loaded(tab.model))
    cust = tab.model.raw_value(0, 2)
    window._follow_foreign_key("customers", "id", cust)
    target = window.tabs.currentWidget()
    assert target.ident.name == "customers"
    assert pump(qapp, lambda: target.model.real_row_count() == 1 and target.model.row_available(0), 8)
    assert target.model.raw_value(0, 1) == cust


def test_sql_tab_results_and_errors(window, qapp):
    tab = window.open_sql_tab("SELECT country, COUNT(*) FROM customers GROUP BY 1;\nSELECT 1 +;")
    from PySide6.QtGui import QTextCursor
    tab.editor.moveCursor(QTextCursor.MoveOperation.Start)
    tab.execute_current()
    assert pump(qapp, lambda: not tab.is_running() and tab.model.rowCount() > 0)
    assert not tab.grid.isColumnHidden(0)
    assert tab.model.headers()[0] == "country"
    tab.execute_all()
    assert pump(qapp, lambda: not tab.is_running() and tab._messages and tab._messages[-1][1])
    assert "syntax error" in tab._messages[-1][0]


def test_create_table_dialog(window, qapp):
    from sqliteview.ui.dialogs.edit_table import C_PK, EditTableDialog
    dlg = EditTableDialog(window.db, None, window)
    dlg.name_edit.setText("notes")
    dlg.add_field()
    dlg.add_field()
    dlg.grid.item(0, 0).setText("id")
    dlg.grid.item(0, C_PK).setCheckState(Qt.Checked)
    dlg.grid.item(1, 0).setText("body")
    dlg.grid.cellWidget(1, 1).setCurrentText("TEXT")
    assert 'PRIMARY KEY("id")' in dlg.preview.toPlainText()
    dlg.accept()
    t = window.db.get_table_by_name(sqlb.ObjectIdentifier("main", "notes"))
    assert t is not None and t.field_names() == ["id", "body"] and t.primary_key_names() == ["id"]


def test_create_index_and_add_record_dialogs(window, qapp):
    from sqliteview.ui.dialogs.add_record import AddRecordDialog
    from sqliteview.ui.dialogs.edit_index import EditIndexDialog
    dlg = EditIndexDialog(window.db, sqlb.ObjectIdentifier("main", "customers"), window)
    dlg.name.setText("idx_email")
    dlg.cols.item(2, 0).setCheckState(Qt.Checked)
    dlg.unique.setChecked(True)
    dlg.accept()
    assert window.db.get_index_by_name(sqlb.ObjectIdentifier("main", "idx_email")).unique
    rec = AddRecordDialog(window.db, sqlb.ObjectIdentifier("main", "customers"), window)
    rec.grid.item(1, 2).setText("Zed")  # name
    rec.grid.item(5, 2).setText("NULL")  # meta
    rec.accept()
    row = window.db.get_row(sqlb.ObjectIdentifier("main", "customers"), 501)
    assert row[1] == "Zed" and row[3] == "IN"  # default country applied


def test_export_csv_and_json(window, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from sqliteview.ui.dialogs.export_data import ExportDataDialog
    out_csv = str(tmp_path / "products.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (out_csv, "")))
    dlg = ExportDataDialog(window.db, "csv", sqlb.ObjectIdentifier("main", "products"), None, window)
    dlg.accept()
    lines = open(out_csv, encoding="utf-8").read().splitlines()
    assert lines[0] == '"sku","title","price","stock","image"' and len(lines) == 41
    out_json = str(tmp_path / "products.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (out_json, "")))
    dlg = ExportDataDialog(window.db, "json", sqlb.ObjectIdentifier("main", "products"), None, window)
    dlg.accept()
    data = json.load(open(out_json, encoding="utf-8"))
    assert len(data) == 40 and isinstance(data[0]["price"], float)


def test_import_csv_dialog(window, qapp, tmp_path):
    from sqliteview.ui.dialogs.import_csv import ImportCsvDialog
    path = tmp_path / "visits.csv"
    path.write_bytes(b"city;count\nPune;3\nDelhi;12\n")
    dlg = ImportCsvDialog(window.db, [str(path)], window)
    dlg.sep.setCurrentIndex(dlg.sep.findData(";"))
    dlg.update_preview()
    assert dlg.preview.columnCount() == 2 and dlg.preview.rowCount() == 2
    dlg.accept()
    assert dlg.imported_tables == ["visits"]
    t = window.db.get_table_by_name(sqlb.ObjectIdentifier("main", "visits"))
    assert [(f.name, f.type) for f in t.fields] == [("city", "TEXT"), ("count", "INTEGER")]
    assert window.db.query_single_value("SELECT SUM(count) FROM visits") == 15
    # importing the same file again appends to the existing table (the question is answered Yes)
    dlg = ImportCsvDialog(window.db, [str(path)], window)
    dlg.sep.setCurrentIndex(dlg.sep.findData(";"))
    dlg.accept()
    assert window.db.query_single_value("SELECT COUNT(*) FROM visits") == 4


def test_connect_ai_dialog(window, qapp):
    from sqliteview.mcp.clientconfig import CLIENTS
    from sqliteview.ui.dialogs.connect_ai import ConnectAiDialog
    assert window.act_connect_ai.isEnabled()
    dlg = ConnectAiDialog(window.db.cur_file, False, window.act_live_ai, window)
    assert json.loads(dlg.snippet.toPlainText())["mcpServers"]["sqliteview"]["args"][-2:] == ["--db", window.db.cur_file]
    dlg.read_only.setChecked(True)
    dlg.clients.button(1).click()
    assert dlg.snippet.toPlainText().startswith("claude mcp add") and "--read-only" in dlg.snippet.toPlainText()
    assert dlg.where.text() == CLIENTS[1].where
    dlg.use_db.setChecked(False)
    assert "--db" not in dlg.snippet.toPlainText()
    dlg.copy()
    assert QGuiApplication.clipboard().text() == dlg.snippet.toPlainText()


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_live_ai_mode(window, qapp):
    import httpx
    from sqliteview.core.settings import Settings
    from sqliteview.mcp.live import live_token, live_url
    from sqliteview.ui.dialogs.connect_ai import MODE_LIVE, ConnectAiDialog
    from sqliteview.ui.table_tab import TableTab
    Settings.set("mcp", "live_port", _free_port())
    tab = window.open_table(sqlb.ObjectIdentifier("main", "customers"))
    assert pump(qapp, loaded(tab.model))
    dlg = ConnectAiDialog(window.db.cur_file, False, window.act_live_ai, window)
    dlg.show()
    assert dlg.mode.current() != MODE_LIVE  # live mode is off
    dlg.mode.button(MODE_LIVE).click()
    assert "--live" in dlg.snippet.toPlainText() and dlg.live_button.isVisible()
    try:
        dlg.live_button.click()  # turns on Tools > Allow AI Assistants
        assert window.act_live_ai.isChecked() and window.live_server is not None
        assert window.status_ai.isVisible() and not dlg.live_button.isVisible()
        assert Settings.get("mcp", "live_enabled")
        reply = httpx.post(live_url(), timeout=30, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "insert_rows", "arguments": {"table": "customers", "rows": [{"name": "From AI"}]}}},
            headers={"Authorization": f"Bearer {live_token()}", "Accept": "application/json, text/event-stream"})
        assert reply.status_code == 200 and '"inserted": 1' in reply.json()["result"]["content"][0]["text"]
        assert pump(qapp, lambda: window.act_write.isEnabled() and tab.model.total_rows() == 501)
        assert isinstance(window.tabs.currentWidget(), TableTab)
        window.write_changes()  # the user keeps the AI's change
        con = sqlite3.connect(window.db.cur_file)
        assert con.execute("SELECT COUNT(*) FROM customers WHERE name = 'From AI'").fetchone()[0] == 1
        con.close()
    finally:
        window.act_live_ai.trigger()  # off again
    assert window.live_server is None and not window.status_ai.isVisible()
    assert not Settings.get("mcp", "live_enabled")


def test_busy_prompt_never_aborts_an_ai_write(window, qapp, monkeypatch):
    from sqliteview.mcp.session import AI_READING, AI_WRITING
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a) or QMessageBox.Yes))
    assert window._ask_interrupt(AI_WRITING) is False and asked == []  # wait instead of rolling back
    assert window._ask_interrupt(AI_READING) is True and len(asked) == 1  # a read may be stopped


def test_live_ai_port_in_use(window, qapp):
    import socket
    from sqliteview.core.settings import Settings
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        Settings.set("mcp", "live_port", busy.getsockname()[1])
        window.act_live_ai.trigger()
    assert window.live_server is None and not window.act_live_ai.isChecked()
    assert window.warnings and "Another SQliteView window" in window.warnings[-1][2]
    assert not Settings.get("mcp", "live_enabled")


def test_drop_and_revert(window, qapp):
    window.open_table(sqlb.ObjectIdentifier("main", "orders"))
    window.drop_object("table", sqlb.ObjectIdentifier("main", "orders"))
    pump(qapp, timeout=0.3)
    assert window.db.get_table_by_name(sqlb.ObjectIdentifier("main", "orders")) is None
    assert all(getattr(window.tabs.widget(i), "ident", None) != sqlb.ObjectIdentifier("main", "orders")
               for i in range(window.tabs.count()))
    window.db.revert_all()
    window.db.update_schema()
    assert window.db.get_table_by_name(sqlb.ObjectIdentifier("main", "orders")) is not None


def test_command_palette_lists_tables(window, qapp):
    entries = window._palette_entries()
    titles = {e.title for e in entries}
    assert {"customers", "orders", "products", "order_summary"} <= titles
    assert "New SQL Editor" in titles and "Compact Database (VACUUM)" in titles


def test_read_only_mode(qapp, demo_db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    from sqliteview.ui.main_window import MainWindow
    w = MainWindow()
    assert w.open_database(demo_db, read_only=True)
    tab = w.open_table(sqlb.ObjectIdentifier("main", "customers"))
    assert pump(qapp, loaded(tab.model))
    assert not tab.model.is_editable()
    assert not w.act_create_table.isEnabled()
    assert w.status_ro.isVisibleTo(w)
    w.close()


def test_full_screen_results_and_table(window, qapp):
    tab = window.open_sql_tab("SELECT * FROM customers;")
    tab.execute_all()
    assert pump(qapp, lambda: not tab.is_running() and tab.model.rowCount() > 0)
    tab.btn_expand.click()
    assert window._full_screen and not tab.editor_box.isVisible() and not window.toolbar.isVisible()
    assert not window.dock_explorer.isVisible() and tab.grid.isVisible()
    window.act_exit_full_screen.trigger()
    assert not window._full_screen and tab.editor_box.isVisible() and window.toolbar.isVisible()
    assert window.dock_explorer.isVisible() and not tab.btn_expand.isChecked()
    ttab = window.open_table(sqlb.ObjectIdentifier("main", "customers"))
    window.act_full_screen.trigger()
    assert window._full_screen and ttab.is_expanded()
    window.act_full_screen.trigger()
    assert not window._full_screen and not ttab.is_expanded()


def test_schema_tab_all_and_single(window, qapp):
    from sqliteview.ui.schema_tab import SchemaTab
    tab = window.open_schema_tab(None)
    assert isinstance(tab, SchemaTab)
    sql = tab.editor.toPlainText()
    for name in ("customers", "orders", "products", "order_summary", "idx_orders_customer", "trg_stock"):
        assert name in sql
    con = sqlite3.connect(":memory:")
    con.executescript(sql)  # the generated schema must be runnable
    assert tab.tree.topLevelItemCount() == 4
    single = window.open_schema_tab(sqlb.ObjectIdentifier("main", "orders"))
    assert single is tab and window.tabs.tabText(window.tabs.indexOf(tab)) == "Schema — orders"
    sql = tab.editor.toPlainText()
    assert "CREATE TABLE orders" in sql and "idx_orders_customer" in sql and "trg_stock" in sql
    assert "CREATE TABLE customers" not in sql
    assert tab.tree.topLevelItemCount() == 1 and tab.tree.topLevelItem(0).childCount() == 6
    tab.related.setChecked(False)
    assert "idx_orders_customer" not in tab.editor.toPlainText()
