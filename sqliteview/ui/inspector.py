"""Cell inspector panel. Port of upstream ``EditDialog`` (the "Edit Database Cell" dock).

Modes: Text, JSON (validated + pretty-printed), Hex (editable as hex digits) and Image.
Values are written back through the model exactly like the original: TEXT unless the column
affinity is numeric, BLOB when binary content (hex/image/imported file) is applied.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QBuffer, QModelIndex, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from ..core.data import image_format, is_binary, value_to_text
from ..core.table_model import RawValueRole, SqliteTableModel
from .theme import mono_font
from .widgets import EmptyState, label, push_button, tool_button

MODES = ["Text", "JSON", "Hex", "Image"]


def hex_dump(data: bytes, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08X}  {hx:<{width * 3}} {asc}")
    return "\n".join(lines)


def parse_hex(text: str) -> bytes:
    """Accept either a hex dump produced by ``hex_dump`` or plain hex digits."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 8 and all(c in "0123456789ABCDEFabcdef" for c in parts[0]) and len(parts) > 1:
            parts = parts[1:]
            # drop the ASCII column (it isn't made of 2-digit hex tokens)
            hexes = []
            for p in parts:
                if len(p) == 2 and all(c in "0123456789ABCDEFabcdef" for c in p):
                    hexes.append(p)
                else:
                    break
            out.append("".join(hexes))
        else:
            out.append("".join(parts))
    return bytes.fromhex("".join(out))


class CellInspector(QWidget):
    statusMessage = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model: Optional[SqliteTableModel] = None
        self.index = QModelIndex()
        self._value: Any = None
        self._is_null = True
        self._dirty = False
        self._loading = False
        self._pending_blob: Optional[bytes] = None
        self._build()
        self._set_enabled(False)

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self.title = label("No cell selected", "h2")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.title, 1)
        self.type_chip = label("", "chip")
        head.addWidget(self.type_chip)
        lay.addLayout(head)
        self.meta = label("", "faint")
        lay.addWidget(self.meta)

        modes = QHBoxLayout()
        modes.addWidget(label("Mode", "muted"))
        self.mode = QComboBox()
        self.mode.addItems(MODES)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        modes.addWidget(self.mode)
        modes.addStretch(1)
        self.btn_format = tool_button("format", "Pretty-print JSON", on_click=self._format_json)
        self.btn_wrap = tool_button("wrap", "Word wrap", checkable=True, on_click=self._toggle_wrap)
        self.btn_wrap.setChecked(True)
        self.btn_import = tool_button("upload", "Import from file…", on_click=self._import_file)
        self.btn_export = tool_button("download", "Export to file…", on_click=self._export_file)
        for b in (self.btn_format, self.btn_wrap, self.btn_import, self.btn_export):
            modes.addWidget(b)
        lay.addLayout(modes)

        self.stack = QStackedWidget()
        self.text = QPlainTextEdit()
        self.text.setFont(mono_font())
        self.text.textChanged.connect(self._on_edit)
        self.stack.addWidget(self.text)
        self.image_area = QScrollArea()
        self.image_area.setWidgetResizable(True)
        self.image_area.setAlignment(Qt.AlignCenter)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_area.setWidget(self.image_label)
        self.stack.addWidget(self.image_area)
        self.empty = EmptyState("edit", "Nothing selected", "Select a cell in a table or result grid to view and edit it here.")
        self.empty.finish()
        self.stack.addWidget(self.empty)
        lay.addWidget(self.stack, 1)

        self.error = label("", "error", wrap=True)
        self.error.hide()
        lay.addWidget(self.error)

        btns = QHBoxLayout()
        self.btn_null = push_button("Set NULL", icon_name="null", on_click=self._set_null)
        btns.addWidget(self.btn_null)
        btns.addStretch(1)
        self.btn_revert = tool_button("undo", "Revert to the stored value", on_click=self._reload)
        self.btn_apply = push_button("Apply", kind="primary", icon_name="check", on_click=self.apply)
        self.btn_apply.setToolTip("Apply changes to the cell (Ctrl+Return)")
        btns.addWidget(self.btn_revert)
        btns.addWidget(self.btn_apply)
        lay.addLayout(btns)
        sc = QShortcut(QKeySequence("Ctrl+Return"), self.text)
        sc.setContext(Qt.WidgetShortcut)
        sc.activated.connect(self.apply)

    # ------------------------------------------------------------------ state
    def _set_enabled(self, on: bool) -> None:
        for w in (self.mode, self.btn_format, self.btn_wrap, self.btn_import, self.btn_export, self.btn_null,
                  self.btn_apply, self.btn_revert):
            w.setEnabled(on)
        if not on:
            self.stack.setCurrentWidget(self.empty)

    def _editable(self) -> bool:
        return self.model is not None and self.index.isValid() and self.model.is_editable(self.index)

    def set_cell(self, model: SqliteTableModel, index: QModelIndex) -> None:
        if self._dirty and self.model is not None and self.index.isValid() and (model is not self.model or index != self.index):
            r = QMessageBox.question(self, "Unapplied changes", "Apply your changes to the previous cell first?",
                                     QMessageBox.Apply | QMessageBox.Discard)
            if r == QMessageBox.Apply:
                self.apply()
        self.model = model
        self.index = index
        self._reload()

    def clear(self) -> None:
        self.model = None
        self.index = QModelIndex()
        self._dirty = False
        self.title.setText("No cell selected")
        self.type_chip.setText("")
        self.meta.setText("")
        self._set_enabled(False)

    def _reload(self) -> None:
        rowid_col = self.model is not None and not self.model.query.table.is_empty() and self.index.column() == 0
        if self.model is None or not self.index.isValid() or rowid_col:
            self.clear()
            return
        m, ix = self.model, self.index
        value = m.data(ix, RawValueRole)
        self._value = value
        self._is_null = value is None
        self._pending_blob = None
        col = m.column_name(ix.column())
        self.title.setText(col)
        table = m.query.table.to_display_string() if not m.query.table.is_empty() else "query result"
        self.meta.setText(f"Row {ix.row() + 1} · {table}")
        self._update_type_chip(value)
        self._set_enabled(True)
        editable = self._editable()
        self.text.setReadOnly(not editable)
        for b in (self.btn_null, self.btn_apply, self.btn_import, self.btn_revert):
            b.setEnabled(editable)
        # pick a mode automatically (upstream "auto switch mode")
        if isinstance(value, (bytes, bytearray)) and image_format(value):
            mode = 3
        elif isinstance(value, (bytes, bytearray)) and is_binary(value):
            mode = 2
        elif isinstance(value, str) and value[:1] in "{[" and self._looks_json(value):
            mode = 1
        else:
            mode = 0
        self._loading = True
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(mode)
        self.mode.blockSignals(False)
        self._show_value()
        self._loading = False
        self._dirty = False
        self.error.hide()

    @staticmethod
    def _looks_json(s: str) -> bool:
        try:
            json.loads(s)
            return True
        except ValueError:
            return False

    def _update_type_chip(self, value: Any) -> None:
        if value is None:
            t, extra = "NULL", ""
        elif isinstance(value, bool) or isinstance(value, int):
            t, extra = "INTEGER", ""
        elif isinstance(value, float):
            t, extra = "REAL", ""
        elif isinstance(value, (bytes, bytearray)):
            fmt = image_format(value)
            t, extra = ("BLOB", f"{fmt} image · {len(value):,} bytes") if fmt else ("BLOB", f"{len(value):,} bytes")
        else:
            t, extra = "TEXT", f"{len(value):,} chars"
        self.type_chip.setText(t)
        self.type_chip.setProperty("role", "chip-accent" if t != "NULL" else "chip")
        self.type_chip.style().unpolish(self.type_chip)
        self.type_chip.style().polish(self.type_chip)
        if extra:
            self.meta.setText(self.meta.text().split("  ·  ")[0] + "  ·  " + extra)

    def _show_value(self) -> None:
        mode = self.mode.currentIndex()
        v = self._pending_blob if self._pending_blob is not None else self._value
        self.btn_format.setVisible(mode == 1)
        if mode == 3:
            pm = QPixmap()
            if isinstance(v, (bytes, bytearray)) and pm.loadFromData(bytes(v)):
                w, h = pm.width(), pm.height()
                if max(w, h) < 128:
                    # Enlarge tiny images (icons, pixel art) without smoothing so they stay crisp
                    factor = max(1, 128 // max(1, max(w, h)))
                    pm = pm.scaled(w * factor, h * factor, Qt.KeepAspectRatio, Qt.FastTransformation)
                self.image_label.setPixmap(pm)
                self.image_label.setToolTip(f"{w} × {h} px")
                self.meta.setText(self.meta.text().split("  ·  ")[0] + f"  ·  {w} × {h} px · {len(v):,} bytes")
            else:
                self.image_label.setText("Not an image")
            self.stack.setCurrentWidget(self.image_area)
            return
        self.stack.setCurrentWidget(self.text)
        self._loading = True
        if v is None:
            self.text.setPlainText("")
            self.text.setPlaceholderText("NULL")
        elif mode == 2:
            b = bytes(v) if isinstance(v, (bytes, bytearray)) else value_to_text(v).encode("utf-8")
            self.text.setPlainText(hex_dump(b))
        elif mode == 1:
            s = value_to_text(v)
            try:
                s = json.dumps(json.loads(s), indent=2, ensure_ascii=False)
            except ValueError:
                pass
            self.text.setPlainText(s)
        else:
            self.text.setPlainText(value_to_text(v))
        self._loading = False

    def _mode_changed(self, _i: int) -> None:
        if self._dirty and not self._loading:
            # Convert edited content to the new representation
            try:
                self._value = self._current_value()
                self._is_null = self._value is None
            except ValueError as e:
                self._show_error(str(e))
        self._show_value()

    def _on_edit(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._is_null = False
        self.error.hide()
        if self.mode.currentIndex() == 1:
            try:
                json.loads(self.text.toPlainText() or "null")
            except ValueError as e:
                self._show_error(f"Invalid JSON: {e}")

    def _show_error(self, msg: str) -> None:
        self.error.setText(msg)
        self.error.show()

    def _current_value(self) -> Any:
        if self._pending_blob is not None:
            return self._pending_blob
        if self._is_null and not self.text.toPlainText():
            return None
        mode = self.mode.currentIndex()
        if mode == 2:
            try:
                return parse_hex(self.text.toPlainText())
            except ValueError as e:
                raise ValueError(f"Invalid hex data: {e}") from e
        if mode == 1:
            txt = self.text.toPlainText()
            try:
                # store compact JSON like upstream does when the original was compact
                obj = json.loads(txt)
                if isinstance(self._value, str) and "\n" not in self._value:
                    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                pass
            return txt
        if mode == 3:
            return self._value
        return self.text.toPlainText()

    # ------------------------------------------------------------------ actions
    def apply(self) -> None:
        if not self._editable():
            return
        try:
            value = self._current_value()
        except ValueError as e:
            self._show_error(str(e))
            return
        is_blob = isinstance(value, (bytes, bytearray))
        if self.model.set_typed_data(self.index, is_blob, value):
            self._dirty = False
            self.statusMessage.emit("Cell updated")
            self._reload()

    def _set_null(self) -> None:
        if not self._editable():
            return
        if self.model.set_typed_data(self.index, False, None):
            self._dirty = False
            self._reload()

    def _format_json(self) -> None:
        try:
            obj = json.loads(self.text.toPlainText())
        except ValueError as e:
            self._show_error(f"Invalid JSON: {e}")
            return
        self.text.setPlainText(json.dumps(obj, indent=2, ensure_ascii=False))

    def _toggle_wrap(self) -> None:
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth if self.btn_wrap.isChecked() else QPlainTextEdit.NoWrap)

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import cell content")
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            self._show_error(str(e))
            return
        self._pending_blob = data
        self._dirty = True
        self._is_null = False
        self._update_type_chip(data)
        self.mode.setCurrentIndex(3 if image_format(data) else (0 if not is_binary(data) else 2))
        self._show_value()
        self.statusMessage.emit(f"Loaded {os.path.basename(path)} — press Apply to store it")

    def _export_file(self) -> None:
        v = self._value
        if v is None:
            return
        fmt = image_format(v) if isinstance(v, (bytes, bytearray)) else ""
        filt = f"{fmt} image (*.{fmt.lower()});;All files (*)" if fmt else ("Text files (*.txt);;All files (*)" if isinstance(v, str)
                                                                         else "Binary files (*.bin);;All files (*)")
        path, _ = QFileDialog.getSaveFileName(self, "Export cell content", "", filt)
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(bytes(v) if isinstance(v, (bytes, bytearray)) else value_to_text(v).encode("utf-8"))
        except OSError as e:
            self._show_error(str(e))
            return
        self.statusMessage.emit(f"Exported to {os.path.basename(path)}")
