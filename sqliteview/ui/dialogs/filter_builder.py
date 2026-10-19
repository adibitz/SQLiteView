"""Filter builder: pick a column, an operator and value(s) instead of typing filter syntax."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QPushButton, QVBoxLayout)

from ...core.filters import FILTER_OPERATORS, build_filter_text, filter_to_sql_condition, parse_filter_text
from ..widgets import label


class FilterBuilderDialog(QDialog):
    """Returns the filter-row text for one column (empty text clears the filter)."""

    def __init__(self, columns: list[tuple[int, str]], column: int, current_text: str = "",
                 filter_escape: str = "\\", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter")
        self.setMinimumWidth(420)
        self._escape = filter_escape
        self._cleared = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Filter rows", "h2"))
        form = QFormLayout()
        form.setVerticalSpacing(8)
        self.column = QComboBox()
        for idx, name in columns:
            self.column.addItem(name, idx)
        self.column.setCurrentIndex(max(0, self.column.findData(column)))
        self.op = QComboBox()
        for key, text, _n in FILTER_OPERATORS:
            self.op.addItem(text, key)
        self.value = QLineEdit()
        self.value.setPlaceholderText("value")
        self.value2 = QLineEdit()
        self.value2.setPlaceholderText("and")
        form.addRow("Column", self.column)
        form.addRow("Condition", self.op)
        form.addRow("Value", self.value)
        form.addRow("", self.value2)
        lay.addLayout(form)
        self.preview = label("", "faint", wrap=True)
        lay.addWidget(self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply Filter")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        clear = QPushButton("Clear Filter")
        clear.setProperty("kind", "ghost")
        clear.clicked.connect(self._clear)
        bb.addButton(clear, QDialogButtonBox.ResetRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        op, v, v2 = parse_filter_text(current_text)
        self.op.setCurrentIndex(max(0, self.op.findData(op)))
        self.value.setText(v)
        self.value2.setText(v2)
        for w in (self.value, self.value2):
            w.textChanged.connect(self._update)
        self.op.currentIndexChanged.connect(self._update)
        self.column.currentIndexChanged.connect(self._update)
        self._update()
        self.value.setFocus()

    def _arity(self) -> int:
        return next(n for k, _t, n in FILTER_OPERATORS if k == self.op.currentData())

    def _update(self) -> None:
        n = self._arity()
        self.value.setVisible(n >= 1)
        self.value2.setVisible(n == 2)
        cond = filter_to_sql_condition(self.filter_text(), self._escape)
        self.preview.setText(f"WHERE {self.column.currentText()} {cond}" if cond else "No filter")

    def _clear(self) -> None:
        self._cleared = True
        self.accept()

    def selected_column(self) -> int:
        return int(self.column.currentData())

    def filter_text(self) -> str:
        if self._cleared:
            return ""
        return build_filter_text(self.op.currentData(), self.value.text(), self.value2.text())
