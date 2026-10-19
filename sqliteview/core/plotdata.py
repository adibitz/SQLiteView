"""Turning model rows into plottable series. Port of the data part of upstream ``PlotDock``
(``guessDataType`` and the conversion loop in ``updatePlot``); drawing lives in ``ui/plot_dock.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Iterable, Optional

from .data import is_binary, value_to_text

ROW_NUMBER = -1  # pseudo column "Row #"


class AxisType(Enum):
    Numeric = "Numeric"
    DateTime = "Date/Time"
    Date = "Date"
    Time = "Time"
    Label = "Label"


def _parse_temporal(text: str) -> Optional[tuple[AxisType, datetime]]:
    t = text.strip()
    if not t or not t[0].isdigit():
        return None
    try:
        if len(t) == 10:
            return AxisType.Date, datetime.combine(date.fromisoformat(t), time(), timezone.utc)
        if ":" in t[:3] and len(t) <= 15:
            return AxisType.Time, datetime.combine(date(1970, 1, 1), time.fromisoformat(t), timezone.utc)
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return AxisType.DateTime, d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def guess_axis_type(values: Iterable[Any]) -> Optional[AxisType]:
    """Common type of a column's non-NULL values; None when nothing is plottable (empty or BLOB)."""
    found: Optional[AxisType] = None
    for v in values:
        if v is None:
            continue
        if is_binary(v):
            return None
        if _as_float(v) is not None:
            kind = AxisType.Numeric
        else:
            parsed = _parse_temporal(value_to_text(v))
            kind = parsed[0] if parsed else AxisType.Label
        if found is None or found == kind:
            found = kind
        elif {found, kind} == {AxisType.Date, AxisType.DateTime}:
            found = AxisType.DateTime
        else:
            return AxisType.Label
    return found


def to_axis_value(value: Any, axis: AxisType) -> Optional[float]:
    """Numeric position of ``value`` on an axis; dates become milliseconds since the epoch (UTC)."""
    if value is None:
        return None
    if axis == AxisType.Numeric:
        return _as_float(value)
    if axis in (AxisType.DateTime, AxisType.Date, AxisType.Time):
        parsed = _parse_temporal(value_to_text(value))
        return parsed[1].timestamp() * 1000.0 if parsed else None
    return None


@dataclass
class PlotData:
    x_type: AxisType
    xs: list[float] = field(default_factory=list)  # x position per row (row index for labels)
    labels: list[str] = field(default_factory=list)  # x text per row, used for Label axes and bars
    ys: dict[int, list[Optional[float]]] = field(default_factory=dict)  # column -> y per row (None = gap)


def build_plot_data(rows: list[list[Any]], x: int, ys: list[int], x_type: Optional[AxisType] = None) -> PlotData:
    """Rows -> points. ``x`` may be ``ROW_NUMBER``; rows whose x can't be placed are skipped."""
    if x == ROW_NUMBER:
        x_type = AxisType.Numeric
    elif x_type is None:
        x_type = guess_axis_type(r[x] for r in rows) or AxisType.Label
    out = PlotData(x_type, ys={c: [] for c in ys})
    for i, r in enumerate(rows):
        raw_x = i + 1 if x == ROW_NUMBER else r[x]
        if x_type == AxisType.Label:
            xv: Optional[float] = float(len(out.xs))
        else:
            xv = to_axis_value(raw_x, x_type)
        if xv is None:
            continue
        out.xs.append(xv)
        out.labels.append("NULL" if raw_x is None else value_to_text(raw_x))
        for c in ys:
            out.ys[c].append(_as_float(r[c]))
    return out
