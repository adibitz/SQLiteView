"""Display formats, conditional formats and the plot dock (engine + UI, offscreen)."""
from __future__ import annotations

from PySide6.QtCharts import QBarSeries, QDateTimeAxis, QLineSeries, QScatterSeries
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from sqliteview.core import sqlb
from sqliteview.core.condformat import CondFormat, CondFormatMatcher, add_cond_format
from sqliteview.core.display_format import match_preset, preset_selector, validate_display_format
from sqliteview.core.plotdata import ROW_NUMBER, AxisType, build_plot_data, guess_axis_type, to_axis_value
from test_ui import demo_db, loaded, pump, qapp, window  # noqa: F401 - shared fixtures

PRODUCTS = sqlb.ObjectIdentifier("main", "products")
CUSTOMERS = sqlb.ObjectIdentifier("main", "customers")


# ---------------------------------------------------------------------------- conditional formats (engine)

def test_cond_format_matching_uses_filter_syntax():
    m = CondFormatMatcher()
    assert m.matches(CondFormat(">10", bg="#ff0000"), 42)
    assert m.matches(CondFormat(">10", bg="#ff0000"), "42")  # numeric text compares as a number, like upstream
    assert not m.matches(CondFormat(">10", bg="#ff0000"), 7)
    assert m.matches(CondFormat("1~5"), 3.5)
    assert m.matches(CondFormat("an"), "Banana")  # LIKE '%an%'
    assert not m.matches(CondFormat("=an"), "Banana")
    assert m.matches(CondFormat("/^B.n/"), "Banana")
    assert m.matches(CondFormat(""), "anything")  # empty filter = every cell
    assert not m.matches(CondFormat("/[/"), "x")  # invalid regex: rule doesn't apply, no exception
    assert not m.matches(CondFormat("%"), "x")  # filter without a condition never matches


def test_cond_format_first_match_and_insert_order():
    a, b, catch_all = CondFormat(">100", fg="#111111"), CondFormat(">10", fg="#222222"), CondFormat("", bg="#333333")
    formats = add_cond_format([], catch_all)
    formats = add_cond_format(formats, b)
    formats = add_cond_format(formats, a)
    assert formats == [a, b, catch_all]  # new conditions go first, the catch-all stays last
    replaced = add_cond_format(formats, CondFormat(">10", fg="#999999"))
    assert [f.fg for f in replaced] == ["#111111", "#999999", ""]  # same condition is replaced in place
    m = CondFormatMatcher()
    assert m.first_match(formats, 500) is a
    assert m.first_match(formats, 50) is b
    assert m.first_match(formats, 1) is catch_all


# ---------------------------------------------------------------------------- display formats (engine)

def test_display_format_presets_round_trip():
    assert preset_selector("upper", "na me") == 'upper("na me")'
    assert preset_selector("default", "x") == '"x"'
    assert match_preset('upper("x")', "x") == "upper"
    assert match_preset('"x"', "x") == "default"
    assert match_preset("", "x") == "default"
    assert match_preset("substr(\"x\", 1, 2)", "x") == "custom"


def test_validate_display_format(window):
    db = window.db
    assert validate_display_format(db, CUSTOMERS, "name", 'upper("name")') == ""
    assert "function call" in validate_display_format(db, CUSTOMERS, "name", "42")
    assert "returned 2" in validate_display_format(db, CUSTOMERS, "name", 'upper("name"), lower("name")')
    assert "database engine" in validate_display_format(db, CUSTOMERS, "name", 'nosuchfn("name")')


# ---------------------------------------------------------------------------- plot data (engine)

def test_guess_axis_type():
    assert guess_axis_type([1, 2.5, None, "3"]) == AxisType.Numeric
    assert guess_axis_type(["2024-01-02", "2024-03-04"]) == AxisType.Date
    assert guess_axis_type(["2024-01-02", "2024-03-04 10:00:00"]) == AxisType.DateTime
    assert guess_axis_type(["10:15:00"]) == AxisType.Time
    assert guess_axis_type(["IN", 3]) == AxisType.Label
    assert guess_axis_type([None, None]) is None
    assert guess_axis_type([b"\x00\x01"]) is None


def test_build_plot_data():
    rows = [[1, "b", 10, "2024-01-01"], [2, "a", None, "2024-01-02"], [3, "c", "x", None]]
    d = build_plot_data(rows, ROW_NUMBER, [2])
    assert d.xs == [1.0, 2.0, 3.0] and d.ys[2] == [10.0, None, None]
    d = build_plot_data(rows, 1, [0])
    assert d.x_type == AxisType.Label and d.xs == [0.0, 1.0, 2.0] and d.labels == ["b", "a", "c"]
    d = build_plot_data(rows, 3, [0])
    assert d.x_type == AxisType.Date and len(d.xs) == 2  # the NULL date is skipped
    assert d.xs[1] - d.xs[0] == 86_400_000
    assert to_axis_value("1970-01-01", AxisType.Date) == 0.0


# ---------------------------------------------------------------------------- model + UI

def test_display_format_in_table_model(window, qapp):
    tab = window.open_table(CUSTOMERS)
    m = tab.model
    assert pump(qapp, loaded(m))
    col = m.column_index("name")
    original = m.raw_value(0, col)
    m.set_display_format(col, 'upper("name")')
    assert pump(qapp, loaded(m))
    assert m.data(m.index(0, col)) == original.upper()
    assert m.has_display_format(col) and not m.has_display_format(m.column_index("email"))
    assert not m.flags(m.index(0, col)) & Qt.ItemIsEditable  # formatted cells are read-only
    assert m.flags(m.index(0, m.column_index("email"))) & Qt.ItemIsEditable
    assert m.data(m.index(0, col), Qt.ForegroundRole) == m.colors.fmt_fg
    # Filters apply to the formatted value (upstream behaviour)
    total = m.rowCount()
    m.update_filter(col, "=" + original.upper())
    assert pump(qapp, lambda: 0 < m.rowCount() < total and m.row_available(m.rowCount() - 1))
    assert {m.raw_value(r, col) for r in range(m.rowCount())} == {original.upper()}
    m.clear_filters()
    # A reload (e.g. after a schema change) keeps the format
    tab.reload()
    assert pump(qapp, loaded(m))
    assert m.display_format(col) == 'upper("name")'
    m.set_display_format(col, "")
    assert pump(qapp, loaded(m))
    assert m.data(m.index(0, col)) == original and not m.query.selected_columns


def test_cond_formats_in_table_model(window, qapp):
    tab = window.open_table(PRODUCTS)
    m = tab.model
    assert pump(qapp, loaded(m))
    price = m.column_index("price")
    m.set_cond_formats("price", [CondFormat(">400", fg="#aa0000", bg="#ffeeee")])
    for r in range(min(20, m.rowCount())):
        ix = m.index(r, price)
        if m.raw_value(r, price) > 400:
            assert m.data(ix, Qt.BackgroundRole) == QColor("#ffeeee")
            assert m.data(ix, Qt.ForegroundRole) == QColor("#aa0000")
        else:
            assert m.data(ix, Qt.BackgroundRole) is None
    assert m.data(m.index(0, m.column_index("title")), Qt.BackgroundRole) is None  # other columns untouched
    tab.reload()
    assert pump(qapp, loaded(m))
    assert m.cond_formats("price")  # survives refresh
    # A rule with only a background picks legible text for it, whatever the theme's text colour
    m.set_cond_formats("price", [CondFormat("", bg="#fff4c2")])
    assert m.data(m.index(0, price), Qt.ForegroundRole).lightness() < 80
    m.set_cond_formats("price", [CondFormat("", bg="#202020")])
    assert m.data(m.index(0, price), Qt.ForegroundRole).lightness() > 200
    m.set_cond_formats("price", [])
    assert m.data(m.index(0, price), Qt.BackgroundRole) is None


def test_display_format_dialog(window, qapp):
    from sqliteview.ui.dialogs.display_format import DisplayFormatDialog
    dlg = DisplayFormatDialog(window.db, CUSTOMERS, "name", "")
    assert dlg.preset.currentData() == "default" and dlg.selected_format() == ""
    dlg.preset.setCurrentIndex(dlg.preset.findData("lower"))
    assert dlg.selected_format() == 'lower("name")'
    dlg.expr.setText("bad sql")
    dlg.expr.textEdited.emit("bad sql")
    assert dlg.preset.currentData() == "custom"
    dlg.accept()
    assert dlg.result() == 0 and "function call" in dlg.error.text()  # stays open with an inline error
    dlg2 = DisplayFormatDialog(window.db, CUSTOMERS, "name", 'upper("name")')
    assert dlg2.preset.currentData() == "upper"


def test_cond_format_dialog(qapp):
    from sqliteview.ui.dialogs.cond_format import CondFormatDialog
    dlg = CondFormatDialog("price", [CondFormat(">1", bg="#00ff00"), CondFormat("", fg="#0000ff")])
    assert [f.filter for f in dlg.formats()] == [">1", ""]
    dlg.table.selectRow(1)
    dlg._move(-1)
    assert [f.filter for f in dlg.formats()] == ["", ">1"]
    dlg.add_rule()
    dlg.table.item(2, 0).setText("=5")
    assert dlg.formats()[2].filter == "=5" and dlg.formats()[2].bg  # new rules get a background
    dlg.table.item(2, 0).setText("%")
    assert "never matches" in dlg.error.text()
    dlg.table.selectRow(0)
    dlg.remove_rule()
    assert [f.filter for f in dlg.formats()] == [">1", "%"]


def test_plot_dock_follows_tabs_and_draws(window, qapp):
    assert window.dock_plot.toggleViewAction() in window.menuBar().actions()[2].menu().actions()  # View menu
    window.dock_plot.show()
    plot = window.plot
    tab = window.open_table(PRODUCTS)
    assert pump(qapp, loaded(tab.model))
    plot.refresh()
    assert plot.model is tab.model
    m = tab.model
    price, stock, title = m.column_index("price"), m.column_index("stock"), m.column_index("title")
    plot.set_axes(ROW_NUMBER, [price, stock, title])
    assert plot.y_columns() == [price, stock]  # text columns can't be Y
    series = plot.chart.series()
    assert len(series) == 2 and all(isinstance(s, QLineSeries) for s in series)
    assert series[0].count() == m.rowCount()
    assert plot.chart.legend().isVisible()
    # Colours follow the column: removing price doesn't repaint stock
    stock_color = series[1].pen().color().name()
    plot.set_axes(ROW_NUMBER, [stock])
    assert plot.chart.series()[0].pen().color().name() == stock_color
    assert not plot.chart.legend().isVisible()
    plot.kind.set_current(1)
    plot.kind.changed.emit(1)
    assert isinstance(plot.chart.series()[0], QScatterSeries)
    plot.kind.set_current(2)
    plot.kind.changed.emit(2)
    assert isinstance(plot.chart.series()[0], QBarSeries)
    # Text X axis -> categories; switching tabs follows the current grid
    plot.kind.set_current(0)
    plot.set_axes(title, [stock])
    assert plot.chart.series()[0].count() == m.rowCount()
    orders = window.open_table(sqlb.ObjectIdentifier("main", "orders"))
    assert pump(qapp, loaded(orders.model))
    assert plot.model is orders.model
    plot.refresh()
    om = orders.model
    plot.set_axes(om.column_index("ordered_at"), [om.column_index("quantity")])
    assert isinstance(plot.chart.axes(Qt.Horizontal)[0], QDateTimeAxis)


def test_plot_sql_results_and_theme(window, qapp):
    from sqliteview.ui.theme import DARK, LIGHT, theme
    tab = window.open_sql_tab("SELECT 1 AS x, 10 AS y UNION ALL SELECT 2, 20 UNION ALL SELECT 3, 15;")
    tab.execute_all()
    assert pump(qapp, lambda: not tab.is_running() and tab.model.rowCount() == 3 and tab.model.row_available(2))
    plot = window.plot
    assert plot.model is tab.model
    plot.refresh()
    plot.set_axes(0, [1])
    s = plot.chart.series()[0]
    assert [(p.x(), p.y()) for p in s.points()] == [(1, 10), (2, 20), (3, 15)]
    assert s.pen().color().name() == LIGHT.chart_series[0]
    try:
        theme.tokens = DARK
        plot._on_theme_changed()
        assert plot.chart.series()[0].pen().color().name() == DARK.chart_series[0]
        assert plot.chart.backgroundBrush().color().name() == DARK.surface
    finally:
        theme.tokens = LIGHT
        plot._on_theme_changed()
    window.close_tab(window.tabs.indexOf(tab))
    pump(qapp, timeout=0.1)
    assert plot.model is not tab.model
