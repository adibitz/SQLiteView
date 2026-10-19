"""Preferences. Port of upstream ``PreferencesDialog`` (the subset that applies to this edition)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFontComboBox, QFormLayout,
                               QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from ...core.settings import Settings
from ..icons import icon
from ..theme import mono_font
from ..widgets import label, push_button


def _page(title: str) -> tuple[QWidget, QFormLayout]:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(20, 14, 20, 14)
    lay.addWidget(label(title, "title"))
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignRight)
    form.setVerticalSpacing(10)
    form.setHorizontalSpacing(14)
    lay.addLayout(form)
    lay.addStretch(1)
    return w, form


class PreferencesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(820, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 12)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        self.nav.setIconSize(self.nav.iconSize())
        self.pages = QStackedWidget()
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self._setters = []
        self._build_general()
        self._build_database()
        self._build_browser()
        self._build_editor()
        self._build_extensions()
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.button(QDialogButtonBox.Ok).setText("Save")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore_defaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.setContentsMargins(0, 0, 14, 0)
        outer.addWidget(bb)

    def _add_page(self, title: str, icon_name: str) -> QFormLayout:
        w, form = _page(title)
        self.pages.addWidget(w)
        self.nav.addItem(QListWidgetItem(icon(icon_name), title))
        return form

    # helpers that bind a widget to a setting
    def _bool(self, form, text, group, name):
        w = QCheckBox(text)
        w.setChecked(bool(Settings.get(group, name)))
        form.addRow("", w)
        self._setters.append(lambda: Settings.set(group, name, w.isChecked()))
        return w

    def _int(self, form, text, group, name, lo, hi, suffix=""):
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setValue(int(Settings.get(group, name)))
        if suffix:
            w.setSuffix(suffix)
        form.addRow(text, w)
        self._setters.append(lambda: Settings.set(group, name, w.value()))
        return w

    def _text(self, form, text, group, name):
        w = QLineEdit(str(Settings.get(group, name) or ""))
        form.addRow(text, w)
        self._setters.append(lambda: Settings.set(group, name, w.text()))
        return w

    def _choice(self, form, text, group, name, choices):
        w = QComboBox()
        for label_, value in choices:
            w.addItem(label_, value)
        cur = Settings.get(group, name)
        idx = max(0, w.findData(cur))
        w.setCurrentIndex(idx)
        form.addRow(text, w)
        self._setters.append(lambda: Settings.set(group, name, w.currentData()))
        return w

    def _font(self, form, text, group, name, monospace: bool = True):
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
        w = QFontComboBox()
        if monospace:
            w.setFontFilters(QFontComboBox.MonospacedFonts)
        w.setCurrentFont(mono_font() if monospace else QApplication.font())
        stored = Settings.get(group, name)
        if stored and stored in QFontDatabase.families():
            w.setCurrentFont(QFont(stored))
        form.addRow(text, w)
        self._setters.append(lambda: Settings.set(group, name, w.currentFont().family()))
        return w

    # pages
    def _build_general(self) -> None:
        f = self._add_page("General", "settings")
        self._choice(f, "Theme", "General", "theme", [("Follow system", "system"), ("Light", "light"), ("Dark", "dark")])
        self._choice(f, "Density", "General", "uiDensity", [("Comfortable", "comfortable"), ("Compact", "compact")])
        self._int(f, "Interface font size", "General", "fontsize", 0, 24, " pt (0 = default)")
        self._int(f, "Recent files", "General", "maxRecentFiles", 1, 40)
        self._text(f, "Database file filter", "General", "DBFileExtensions")

    def _build_database(self) -> None:
        f = self._add_page("Database", "database")
        loc = QLineEdit(Settings.get("db", "defaultlocation"))
        row = QHBoxLayout()
        row.addWidget(loc, 1)
        row.addWidget(push_button("Browse…", on_click=lambda: loc.setText(
            QFileDialog.getExistingDirectory(self, "Default location", loc.text()) or loc.text())))
        f.addRow("Default directory", row)
        self._setters.append(lambda: Settings.set("db", "defaultlocation", loc.text()))
        self._choice(f, "New database encoding", "db", "defaultencoding",
                     [("UTF-8", "UTF-8"), ("UTF-16", "UTF-16"), ("UTF-16le", "UTF-16le"), ("UTF-16be", "UTF-16be")])
        self._bool(f, "Enforce foreign keys (PRAGMA foreign_keys)", "db", "foreignkeys")
        self._int(f, "Prefetch block size", "db", "prefetchsize", 100, 1_000_000, " rows")
        sql = QPlainTextEdit(Settings.get("db", "defaultsqltext") or "")
        sql.setFont(mono_font())
        sql.setPlaceholderText("SQL executed after opening a database (e.g. PRAGMA settings)")
        sql.setMaximumHeight(110)
        f.addRow("SQL to run on open", sql)
        self._setters.append(lambda: Settings.set("db", "defaultsqltext", sql.toPlainText()))

    def _build_browser(self) -> None:
        f = self._add_page("Data Browser", "table")
        self._font(f, "Font", "databrowser", "font", monospace=False)
        self._int(f, "Font size", "databrowser", "fontsize", 0, 40, " pt (0 = default)")
        self._text(f, "Text for NULL", "databrowser", "null_text")
        self._text(f, "Text for BLOB", "databrowser", "blob_text")
        self._int(f, "Truncate text after", "databrowser", "symbol_limit", 1, 1_000_000, " chars")
        self._int(f, "Row limit", "databrowser", "rows_limit", 1, 2_000_000_000, " rows")
        self._int(f, "Ask before copying more than", "databrowser", "complete_threshold", 1, 10_000_000, " rows")
        self._text(f, "Filter escape character", "databrowser", "filter_escape")
        self._int(f, "Filter delay", "databrowser", "filter_delay", 0, 5000, " ms")
        self._bool(f, "Show image previews in cells", "databrowser", "image_preview")

    def _build_editor(self) -> None:
        f = self._add_page("SQL Editor", "terminal")
        self._font(f, "Font", "editor", "font")
        self._int(f, "Font size", "editor", "fontsize", 6, 40, " pt")
        self._int(f, "Tab size", "editor", "tabsize", 1, 16)
        self._bool(f, "Wrap long lines", "editor", "wrap_lines")
        self._bool(f, "Auto completion", "editor", "auto_completion")
        self._bool(f, "Upper case keywords in completion", "editor", "upper_keywords")
        self._bool(f, "Highlight errors", "editor", "error_indicators")
        self._choice(f, "Identifier quotes", "editor", "identifier_quotes",
                     [('"Double quotes" (SQL standard)', 0), ("`Grave accents` (MySQL)", 1), ("[Square brackets] (MS SQL)", 2)])

    def _build_extensions(self) -> None:
        f = self._add_page("Extensions", "wrench")
        self._bool(f, "Allow load_extension() from SQL", "extensions", "enable_load_extension")
        self._bool(f, "Disable built-in REGEXP function", "extensions", "disableregex")
        lst = QListWidget()
        for p in Settings.get("extensions", "list") or []:
            lst.addItem(p)
        row = QHBoxLayout()
        row.addWidget(push_button("Add…", icon_name="plus", on_click=lambda: self._add_ext(lst)))
        row.addWidget(push_button("Remove", icon_name="minus", on_click=lambda: lst.takeItem(lst.currentRow())))
        row.addStretch(1)
        box = QVBoxLayout()
        box.addWidget(lst)
        box.addLayout(row)
        f.addRow("Load on open", box)
        self._setters.append(lambda: Settings.set("extensions", "list", [lst.item(i).text() for i in range(lst.count())]))

    def _add_ext(self, lst: QListWidget) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Select extension", "", "Extensions (*.dll *.so *.dylib);;All files (*)")
        if p:
            lst.addItem(p)

    def _restore_defaults(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(self, "Preferences", "Restore all settings to their defaults?") == QMessageBox.Yes:
            recent = Settings.get("General", "recentFileList")
            Settings.reset()
            Settings.set("General", "recentFileList", recent)
            super().accept()

    def accept(self) -> None:
        for s in self._setters:
            s()
        super().accept()
