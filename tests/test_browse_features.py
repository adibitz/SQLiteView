"""Pagination, filter builder, active-filter chips, multi-row selection, row details and row export."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from sqliteview.core import sqlb
from sqliteview.core.filters import build_filter_text, describe_filter, filter_to_sql_condition, parse_filter_text
from sqliteview.core.rowexport import format_for_path, format_rows

from test_ui import demo_db, loaded, pump, qapp, window  # noqa: F401 - shared fixtures

CUSTOMERS = sqlb.ObjectIdentifier("main", "customers")


def open_customers(window, qapp, page_size=100):
    tab = window.open_table(CUSTOMERS)
    tab.page_size.setCurrentIndex(tab.page_size.findData(page_size))
    assert pump(qapp, lambda: tab.model.total_rows() == 500 and loaded(tab.model)())
    return tab


# ---------------------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("op,v1,v2,text", [
    ("contains", "ab", "", "ab"), ("equals", "5", "", "=5"), ("not_equals", "x", "", "<>x"),
    ("starts", "ab", "", "ab%"), ("ends", "ab", "", "%ab"), ("gt", "5", "", ">5"), ("le", "5", "", "<=5"),
    ("between", "1", "9", "1~9"), ("regex", "^a", "", "/^a/"), ("null", "", "", "=NULL"),
    ("not_null", "", "", "<>NULL"), ("empty", "", "", "=''"),
])
def test_filter_builder_round_trip(op, v1, v2, text):
    assert build_filter_text(op, v1, v2) == text
    assert parse_filter_text(text) == (op, v1, v2)
    assert filter_to_sql_condition(text)
    assert describe_filter(text)


def test_filter_builder_incomplete_values_clear_the_filter():
    assert build_filter_text("between", "1", "") == ""
    assert build_filter_text("equals", "") == ""


def test_format_rows():
    names, rows = ["id", "name"], [[1, 'a,"b"'], [2, None]]
    assert format_rows("csv", names, rows, newline="\n") == '"id","name"\n1,"a,""b"""\n2,\n'
    assert format_rows("tsv", names, [[1, "a,b"]], newline="\n").splitlines()[1] == "1\ta,b"
    assert json.loads(format_rows("json", names, rows)) == [{"id": 1, "name": 'a,"b"'}, {"id": 2, "name": None}]
    assert format_rows("sql", names, rows, "t").startswith('INSERT INTO "t" ("id","name") VALUES (1,\'a,"b"\');')
    assert "| 2 | NULL |" in format_rows("md", names, rows)
    assert format_for_path("x.JSON") == "json" and format_for_path("x.txt") == "csv"


# ---------------------------------------------------------------------------- pagination

def test_pagination_pages_and_numbers(window, qapp):
    tab = open_customers(window, qapp, 100)
    m = tab.model
    assert m.rowCount() == 100 and m.page_count() == 5
    assert tab.rows_label.text().startswith("Showing 1–100 of 500")
    tab.btn_next.click()
    assert pump(qapp, loaded(m))
    assert m.page == 1 and m.raw_value(0, 1) == 101 and m.headerData(0, Qt.Vertical) == "101"
    tab.btn_last.click()
    assert pump(qapp, loaded(m))
    assert m.page == 4 and m.raw_value(99, 1) == 500
    assert not tab.btn_next.isEnabled() and tab.btn_prev.isEnabled()
    # switching to "All" keeps the first visible row and shows everything
    tab.page_size.setCurrentIndex(tab.page_size.findData(0))
    assert m.page_size == 0 and m.rowCount() == 500 and not tab.pager.isVisible()


def test_pagination_resets_on_filter_and_goto_row(window, qapp):
    tab = open_customers(window, qapp, 50)
    m = tab.model
    m.set_page(3)
    tab.grid.filter_header.set_filter(4, "IN")
    tab.grid.filter_header._flush()
    assert pump(qapp, lambda: m.row_count_available() == 2 and m.page == 0 and loaded(m)())
    assert "(filtered)" in tab.rows_label.text()
    tab.grid.filter_header.set_filter(4, "")
    tab.grid.filter_header._flush()
    assert pump(qapp, lambda: m.total_rows() == 500)
    tab.goto.setValue(321)
    tab._goto_row()
    assert pump(qapp, loaded(m))
    assert m.page == 6 and tab.grid.currentIndex().row() == 20 and m.raw_value(20, 1) == 321


def test_pagination_edit_insert_delete(window, qapp):
    tab = open_customers(window, qapp, 100)
    m = tab.model
    m.set_page(2)
    assert pump(qapp, loaded(m))
    assert m.setData(m.index(0, 2), "Paged Edit")
    assert window.db.query_single_value("SELECT name FROM customers WHERE id=201") == "Paged Edit"
    tab.add_record()
    assert m.page == 5 and m.rowCount() == 1  # the new 501st row lives on a new last page
    tab.grid.select_cell(0, 1)
    tab.delete_records()
    assert m.total_rows() == 500 and m.page == 4
    assert window.db.query_single_value("SELECT COUNT(*) FROM customers") == 500


def test_reload_keeps_page(window, qapp):
    tab = open_customers(window, qapp, 100)
    tab.model.set_page(3)
    tab.reload()
    assert pump(qapp, lambda: tab.model.page == 3 and loaded(tab.model)())
    assert tab.model.raw_value(0, 1) == 301


# ---------------------------------------------------------------------------- filters, chips, sorting

def test_filter_chips_and_header_sort(window, qapp):
    tab = open_customers(window, qapp, 100)
    tab.grid.filter_header.set_filter(4, "=IN")
    tab.grid.filter_header._flush()
    assert pump(qapp, lambda: tab.model.row_count_available() == 2)
    tab.grid.sort_column(2, 1)  # name descending
    assert pump(qapp, loaded(tab.model))
    assert tab.chips_bar.isVisibleTo(tab)
    chips = [tab.chips_layout.itemAt(i).widget() for i in range(tab.chips_layout.count())
             if tab.chips_layout.itemAt(i).widget() is not None]
    texts = [c.text() for c in chips]
    assert any(t.startswith("country = IN") for t in texts)
    assert any(t.startswith("Sorted by name ↓") for t in texts)
    next(c for c in chips if c.text().startswith("country")).click()
    tab.grid.filter_header._flush()
    assert pump(qapp, lambda: tab.model.total_rows() == 500)
    assert tab.model.query.where == {}


def test_filter_builder_dialog(qapp):
    from sqliteview.ui.dialogs.filter_builder import FilterBuilderDialog
    dlg = FilterBuilderDialog([(1, "id"), (2, "name")], 1, ">10")
    assert dlg.op.currentData() == "gt" and dlg.value.text() == "10"
    dlg.op.setCurrentIndex(dlg.op.findData("between"))
    dlg.value.setText("5")
    dlg.value2.setText("8")
    assert dlg.filter_text() == "5~8" and "BETWEEN 5 AND 8" in dlg.preview.text()
    dlg._clear()
    assert dlg.filter_text() == ""


# ---------------------------------------------------------------------------- selection, details, export

def test_row_selection_copy_and_export(window, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    tab = open_customers(window, qapp, 100)
    tab.btn_rows.click()
    assert tab.grid.row_selection()
    from PySide6.QtCore import QItemSelectionModel
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    tab.grid.selectRow(2)
    for r in (3, 5):
        tab.grid.selectionModel().select(tab.model.index(r, 1), flags)
    assert tab.grid.selection_summary()[0] == 3
    assert tab.sel_bar.isVisibleTo(tab) and tab.sel_label.text() == "3 rows selected"
    tab._copy_rows("json")
    data = json.loads(QGuiApplication.clipboard().text())
    assert [d["id"] for d in data] == [3, 4, 6] and "email" in data[0]
    out = tmp_path / "sel.sql"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    tab.export_selected()
    assert out.read_text(encoding="utf-8").count('INSERT INTO "customers"') == 3
    page = tmp_path / "page.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(page), "")))
    tab.export_page()
    assert len(page.read_text(encoding="utf-8").splitlines()) == 101  # header + one page


def test_row_details_dialog(window, qapp):
    from sqliteview.ui.dialogs.record_view import RecordDialog
    tab = open_customers(window, qapp, 100)
    tab.model.set_page(1)
    assert pump(qapp, loaded(tab.model))
    dlg = RecordDialog(tab.model, 0, 1)
    assert dlg.title.text() == "Row 101 of 500"
    dlg.go(1)
    assert dlg.title.text() == "Row 102 of 500" and dlg.btn_prev.isEnabled()
    dlg.copy_json()
    assert json.loads(QGuiApplication.clipboard().text())[0]["id"] == 102
