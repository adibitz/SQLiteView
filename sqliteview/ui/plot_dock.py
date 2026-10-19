"""Plot panel. Port of upstream ``PlotDock`` using Qt Charts instead of QCustomPlot.

Plots the rows already loaded by the current table tab or SQL result (the grid's
``SqliteTableModel``): one X column (or the row number) and up to eight numeric Y columns as
lines, scatter points or bars. "Load all data" fetches the remaining rows first.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCharts import (QBarCategoryAxis, QBarSeries, QBarSet, QChart, QChartView, QDateTimeAxis, QLineSeries,
                              QScatterSeries, QValueAxis)
from PySide6.QtCore import QDateTime, QMargins, QPointF, Qt, QTimer, QTimeZone
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QSplitter,
                               QStackedWidget, QToolTip, QVBoxLayout, QWidget)

from ..core.plotdata import ROW_NUMBER, AxisType, PlotData, build_plot_data, guess_axis_type
from ..core.table_model import SqliteTableModel
from .theme import theme, tokens
from .widgets import EmptyState, SegmentedControl, label, push_button, tool_button

CHART_LINE, CHART_SCATTER, CHART_BAR = 0, 1, 2
MAX_SERIES = 8  # one per categorical colour; more would need repeated colours
MAX_BARS = 500  # bar categories beyond this are unreadable and slow to lay out
_SLOT_ROLE = Qt.UserRole + 1  # colour slot of a checked Y column
_DATE_FORMATS = {AxisType.DateTime: "yyyy-MM-dd hh:mm", AxisType.Date: "yyyy-MM-dd", AxisType.Time: "hh:mm:ss"}


def _swatch(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(QColor(color))
    return QIcon(pm)


def _display_ms(utc_ms: float) -> float:
    """Dates are parsed as UTC wall time; QDateTimeAxis shows local time, so shift to keep the wall time."""
    d = QDateTime.fromMSecsSinceEpoch(int(utc_ms), QTimeZone(QTimeZone.UTC))
    return float(QDateTime(d.date(), d.time()).toMSecsSinceEpoch())


class PlotPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.model: Optional[SqliteTableModel] = None
        self._types: dict[int, Optional[AxisType]] = {}
        self._headers: list[str] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self._build()
        theme.changed.connect(self._on_theme_changed)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)

        side = QFrame()
        side.setProperty("role", "surface")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 8, 10, 8)
        sl.setSpacing(6)
        self.source = label("", "h2")
        sl.addWidget(self.source)
        self.kind = SegmentedControl([("Line", ""), ("Scatter", ""), ("Bar", "")])
        self.kind.changed.connect(lambda *_: self.redraw())
        sl.addWidget(self.kind)
        sl.addWidget(label("X AXIS", "section"))
        self.x_combo = QComboBox()
        self.x_combo.currentIndexChanged.connect(lambda *_: self.redraw())
        sl.addWidget(self.x_combo)
        sl.addWidget(label("Y AXIS", "section"))
        self.y_list = QListWidget()
        self.y_list.itemChanged.connect(self._y_changed)
        sl.addWidget(self.y_list, 1)
        self.status = label("", "faint", wrap=True)
        sl.addWidget(self.status)
        row = QHBoxLayout()
        self.btn_load = push_button("Load all data", on_click=self.load_all)
        row.addWidget(self.btn_load)
        row.addStretch(1)
        row.addWidget(tool_button("refresh", "Reset zoom", on_click=lambda: self.chart.zoomReset()))
        row.addWidget(tool_button("download", "Save as image…", on_click=self.save_image))
        sl.addLayout(row)
        side.setMinimumWidth(240)
        split.addWidget(side)

        self.stack = QStackedWidget()
        self.empty = EmptyState("chart", "Nothing to plot", "")
        self.stack.addWidget(self.empty)
        self.chart = QChart()
        self.view = QChartView(self.chart)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setRubberBand(QChartView.RectangleRubberBand)
        self.stack.addWidget(self.view)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 600])
        self._show_empty("Open a table or run a query to plot its data.")

    # ------------------------------------------------------------------ model
    def set_model(self, model: Optional[SqliteTableModel], source: str = "") -> None:
        if model is self.model:
            self.source.setText(source)
            return
        if self.model is not None:
            for sig in (self.model.modelReset, self.model.layoutChanged, self.model.finishedFetch,
                        self.model.finishedRowCount, self.model.dataChanged):
                try:
                    sig.disconnect(self._schedule)
                except (RuntimeError, TypeError):
                    pass
        self.model = model
        self.source.setText(source)
        self._headers = []
        if model is not None:
            for sig in (model.modelReset, model.layoutChanged, model.finishedFetch, model.finishedRowCount,
                        model.dataChanged):
                sig.connect(self._schedule)
        self.refresh()

    def _schedule(self, *_) -> None:
        self._timer.start(150)

    def _first_column(self) -> int:
        # Column 0 of a browsed table is the hidden rowid
        m = self.model
        return 1 if m is not None and not m.query.table.is_empty() else 0

    def _loaded_rows(self) -> list[list]:
        m = self.model
        if m is None:
            return []
        rows = []
        for r in range(m.rowCount()):
            v = m.row_values(r)
            if v is None:
                break  # plot the contiguous loaded block, like upstream
            rows.append(v)
        return rows

    def refresh(self) -> None:
        """Re-read columns and types from the model, keeping the user's choices by name."""
        m = self.model
        headers = m.headers() if m is not None else []
        rows = self._loaded_rows()
        first = self._first_column()
        self._types = {c: guess_axis_type(r[c] for r in rows if c < len(r)) for c in range(first, len(headers))}
        if headers != self._headers:
            old_x = self.x_combo.currentText()
            old_y = {self.y_list.item(i).text() for i in range(self.y_list.count())
                     if self.y_list.item(i).checkState() == Qt.Checked}
            self._headers = headers
            self.x_combo.blockSignals(True)
            self.x_combo.clear()
            self.x_combo.addItem("Row #", ROW_NUMBER)
            for c in range(first, len(headers)):
                self.x_combo.addItem(headers[c], c)
            i = self.x_combo.findText(old_x)
            self.x_combo.setCurrentIndex(max(0, i))
            self.x_combo.blockSignals(False)
            self.y_list.blockSignals(True)
            self.y_list.clear()
            for c in range(first, len(headers)):
                it = QListWidgetItem(headers[c])
                it.setData(Qt.UserRole, c)
                it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                it.setCheckState(Qt.Checked if headers[c] in old_y else Qt.Unchecked)
                self.y_list.addItem(it)
            self.y_list.blockSignals(False)
        self._update_y_items()
        self.redraw()

    def _assign_color_slots(self) -> None:
        # A checked column keeps its colour slot until unchecked, so adding or removing other
        # series never repaints it; at most MAX_SERIES slots exist, so colours never repeat.
        items = [self.y_list.item(i) for i in range(self.y_list.count())]
        for it in items:
            if it.checkState() != Qt.Checked:
                it.setData(_SLOT_ROLE, None)
        used = {it.data(_SLOT_ROLE) for it in items if it.data(_SLOT_ROLE) is not None}
        for it in items:
            if it.checkState() == Qt.Checked and it.data(_SLOT_ROLE) is None:
                free = next((n for n in range(MAX_SERIES) if n not in used), None)
                if free is None:
                    it.setCheckState(Qt.Unchecked)
                    continue
                it.setData(_SLOT_ROLE, free)
                used.add(free)

    def _update_y_items(self) -> None:
        self.y_list.blockSignals(True)
        self._assign_color_slots()
        checked = len(self.y_columns())
        for i in range(self.y_list.count()):
            it = self.y_list.item(i)
            t = self._types.get(it.data(Qt.UserRole))
            numeric = t == AxisType.Numeric
            is_checked = it.checkState() == Qt.Checked
            enabled = numeric and (is_checked or checked < MAX_SERIES)
            it.setFlags((Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) if enabled else Qt.ItemIsUserCheckable)
            it.setIcon(_swatch(self._color_of(it.data(Qt.UserRole))) if is_checked else QIcon())
            it.setToolTip(f"{t.value if t else 'No data'}" + ("" if numeric else " — only numeric columns can be plotted"))
        self.y_list.blockSignals(False)

    def _on_theme_changed(self) -> None:
        self._update_y_items()  # swatches use the new mode's colours
        self.redraw()

    def _y_changed(self, *_) -> None:
        self._update_y_items()
        self.redraw()

    def y_columns(self) -> list[int]:
        return [self.y_list.item(i).data(Qt.UserRole) for i in range(self.y_list.count())
                if self.y_list.item(i).checkState() == Qt.Checked
                and self._types.get(self.y_list.item(i).data(Qt.UserRole)) == AxisType.Numeric]

    def set_axes(self, x: int, ys: list[int]) -> None:
        """Select columns programmatically (X may be ``ROW_NUMBER``)."""
        self.x_combo.setCurrentIndex(max(0, self.x_combo.findData(x)))
        self.y_list.blockSignals(True)
        for i in range(self.y_list.count()):
            it = self.y_list.item(i)
            it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in ys else Qt.Unchecked)
        self.y_list.blockSignals(False)
        self._y_changed()

    def load_all(self) -> None:
        if self.model is not None:
            self.model.complete_cache()
            self.refresh()

    def save_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save plot", "plot.png", "PNG (*.png);;JPEG (*.jpg);;BMP (*.bmp)")
        if path:
            self.view.grab().save(path)

    # ------------------------------------------------------------------ drawing
    def _show_empty(self, hint: str) -> None:
        self.empty.subtitle.setText(hint)
        self.stack.setCurrentWidget(self.empty)

    def redraw(self) -> None:
        self.chart.removeAllSeries()
        for ax in self.chart.axes():
            self.chart.removeAxis(ax)
        m = self.model
        self.btn_load.setEnabled(m is not None and not m.is_cache_complete())
        if m is None or not self._headers:
            self.status.setText("")
            self._show_empty("Open a table or run a query to plot its data.")
            return
        rows = self._loaded_rows()
        ys = self.y_columns()
        if not ys:
            self.status.setText(f"{len(rows):,} rows loaded")
            self._show_empty("Choose one or more numeric columns for the Y axis.")
            return
        x = self.x_combo.currentData()
        x = ROW_NUMBER if x is None else x
        data = build_plot_data(rows, x, ys, AxisType.Numeric if x == ROW_NUMBER else self._types.get(x))
        if not data.xs:
            self.status.setText(f"{len(rows):,} rows loaded")
            self._show_empty("The X column has no values that can be plotted.")
            return
        kind = self.kind.current()
        note = ""
        if kind == CHART_BAR and len(data.xs) > MAX_BARS:
            note = f" · bars show the first {MAX_BARS:,}"
        self.status.setText(f"{len(data.xs):,} of {len(rows):,} loaded rows plotted{note}")
        self._draw(data, x, ys, kind)
        self.stack.setCurrentWidget(self.view)

    def _color_of(self, column: int) -> str:
        colors = tokens().chart_series
        for i in range(self.y_list.count()):
            it = self.y_list.item(i)
            if it.data(Qt.UserRole) == column and it.data(_SLOT_ROLE) is not None:
                return colors[it.data(_SLOT_ROLE)]
        return colors[0]

    def _draw(self, data: PlotData, x: int, ys: list[int], kind: int) -> None:
        t = tokens()
        self._style_chart()
        names = {c: self._headers[c] for c in ys}
        x_name = "Row #" if x == ROW_NUMBER else self._headers[x]
        if kind == CHART_BAR:
            n = min(len(data.xs), MAX_BARS)
            series = QBarSeries()
            for c in ys:
                s = QBarSet(names[c])
                s.append([v if v is not None else 0.0 for v in data.ys[c][:n]])
                s.setColor(QColor(self._color_of(c)))
                s.setBorderColor(QColor(t.surface))
                s.hovered.connect(lambda state, i, name=names[c], vals=data.ys[c], labels=data.labels:
                                  self._tip(state, f"{x_name}: {labels[i]}\n{name}: {self._fmt(vals[i])}"))
                series.append(s)
            self.chart.addSeries(series)
            ax = QBarCategoryAxis()
            ax.append(data.labels[:n])
            ax.setLabelsAngle(-45 if n > 12 else 0)
        else:
            pts_order = range(len(data.xs))
            if kind == CHART_LINE and data.x_type != AxisType.Label:
                pts_order = sorted(pts_order, key=lambda i: data.xs[i])  # a line reads left to right
            is_date = data.x_type in _DATE_FORMATS
            xs = [_display_ms(v) for v in data.xs] if is_date else data.xs
            for c in ys:
                s = QLineSeries() if kind == CHART_LINE else QScatterSeries()
                s.setName(names[c])
                color = QColor(self._color_of(c))
                if kind == CHART_LINE:
                    s.setPen(QPen(color, 2))
                else:
                    s.setMarkerSize(8)
                    s.setColor(color)
                    s.setBorderColor(QColor(t.surface))
                vals = data.ys[c]
                s.replace([QPointF(xs[i], vals[i]) for i in pts_order if vals[i] is not None])
                s.hovered.connect(lambda p, state, name=names[c]: self._tip(state, self._point_text(data, p, x_name, name)))
                self.chart.addSeries(s)
            if data.x_type == AxisType.Label:
                ax = QBarCategoryAxis()
                ax.append(data.labels)
                ax.setLabelsVisible(len(data.labels) <= 40)
            elif is_date:
                ax = QDateTimeAxis()
                ax.setFormat(_DATE_FORMATS[data.x_type])
                ax.setTickCount(6)
            else:
                ax = QValueAxis()
                ax.setLabelFormat("%g")
        ay = QValueAxis()
        ay.setLabelFormat("%g")
        # An axis shared by several series only auto-fits the first one, so set the ranges here
        vals = [v for c in ys for v in data.ys[c] if v is not None] or [0.0]
        lo, hi = min(vals), max(vals)
        if kind == CHART_BAR:
            lo, hi = min(0.0, lo), max(0.0, hi)  # bars are anchored at zero
        ay.setRange(lo, hi if hi > lo else lo + 1)
        ay.applyNiceNumbers()
        if isinstance(ax, QValueAxis):
            ax.setRange(min(data.xs), max(data.xs) if max(data.xs) > min(data.xs) else min(data.xs) + 1)
            ax.applyNiceNumbers()
        elif isinstance(ax, QDateTimeAxis):
            ax.setRange(QDateTime.fromMSecsSinceEpoch(int(min(xs))), QDateTime.fromMSecsSinceEpoch(int(max(xs))))
        ax.setTitleText(x_name)
        if len(ys) == 1:
            ay.setTitleText(names[ys[0]])
        for a, align in ((ax, Qt.AlignBottom), (ay, Qt.AlignLeft)):
            self._style_axis(a)
            self.chart.addAxis(a, align)
        for s in self.chart.series():
            s.attachAxis(ax)
            s.attachAxis(ay)
        self.chart.legend().setVisible(len(ys) > 1)

    def _style_chart(self) -> None:
        t = tokens()
        self.chart.setBackgroundBrush(QColor(t.surface))
        self.chart.setBackgroundRoundness(0)
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.view.setBackgroundBrush(QColor(t.surface))
        lg = self.chart.legend()
        lg.setAlignment(Qt.AlignBottom)
        lg.setLabelColor(QColor(t.text_muted))

    @staticmethod
    def _style_axis(axis) -> None:
        t = tokens()
        axis.setLabelsColor(QColor(t.text_muted))
        axis.setTitleBrush(QColor(t.text_muted))
        axis.setLinePen(QPen(QColor(t.border_strong), 1))
        axis.setGridLineColor(QColor(t.grid))
        axis.setShadesVisible(False)
        if hasattr(axis, "setMinorGridLineVisible"):
            axis.setMinorGridLineVisible(False)

    @staticmethod
    def _fmt(v: Optional[float]) -> str:
        return "NULL" if v is None else f"{v:,.6g}"

    def _point_text(self, data: PlotData, p: QPointF, x_name: str, y_name: str) -> str:
        if data.x_type == AxisType.Label:
            i = int(round(p.x()))
            xt = data.labels[i] if 0 <= i < len(data.labels) else ""
        elif data.x_type in _DATE_FORMATS:
            xt = QDateTime.fromMSecsSinceEpoch(int(p.x())).toString(_DATE_FORMATS[data.x_type])
        else:
            xt = self._fmt(p.x())
        return f"{x_name}: {xt}\n{y_name}: {self._fmt(p.y())}"

    def _tip(self, state: bool, text: str) -> None:
        if state:
            QToolTip.showText(QCursor.pos(), text, self.view)
        else:
            QToolTip.hideText()
