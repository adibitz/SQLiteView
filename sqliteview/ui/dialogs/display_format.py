"""Column display format dialog. Port of upstream ``ColumnDisplayFormatDialog``."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QLineEdit, QVBoxLayout

from ...core import sqlb
from ...core.db import DBBrowserDB
from ...core.display_format import PRESETS, match_preset, preset_selector, validate_display_format
from ..theme import mono_font
from ..widgets import label


class DisplayFormatDialog(QDialog):
    def __init__(self, db: DBBrowserDB, table: sqlb.ObjectIdentifier, column: str, current: str = "", parent=None):
        super().__init__(parent)
        self.db = db
        self.table = table
        self.column = column
        self.setWindowTitle("Display Format")
        self.resize(560, 0)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Display Format", "title"))
        lay.addWidget(label(f"Choose how values of column “{column}” are shown. The stored data is not changed; "
                            "formatted cells are read-only.", "muted", wrap=True))
        self.preset = QComboBox()
        self.preset.addItem("Default", "default")
        self.preset.insertSeparator(self.preset.count())
        for p in PRESETS:
            if p is None:
                self.preset.insertSeparator(self.preset.count())
            else:
                self.preset.addItem(p[1], p[0])
        self.preset.insertSeparator(self.preset.count())
        self.preset.addItem("Custom", "custom")
        lay.addWidget(self.preset)
        lay.addWidget(label("SQL EXPRESSION", "section"))
        self.expr = QLineEdit()
        self.expr.setFont(mono_font())
        lay.addWidget(self.expr)
        self.error = label("", "error", wrap=True)
        lay.addWidget(self.error)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

        key = match_preset(current, column)
        self.preset.setCurrentIndex(self.preset.findData(key))
        self.expr.setText(current or sqlb.escape_identifier(column))
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.expr.textEdited.connect(self._expr_edited)

    def _preset_changed(self) -> None:
        key = self.preset.currentData()
        if key != "custom":
            self.expr.setText(preset_selector(key, self.column))
        self.error.clear()

    def _expr_edited(self) -> None:
        # Typing your own SQL turns the choice into "Custom", like upstream's setCustom()
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData(match_preset(self.expr.text().strip(), self.column)))
        self.preset.blockSignals(False)
        self.error.clear()

    def selected_format(self) -> str:
        """The chosen SQL expression, or "" for the default (raw value)."""
        text = self.expr.text().strip()
        return "" if match_preset(text, self.column) == "default" else text

    def accept(self) -> None:
        fmt = self.selected_format()
        if fmt:
            err = validate_display_format(self.db, self.table, self.column, fmt)
            if err:
                self.error.setText(err)
                return
        super().accept()
