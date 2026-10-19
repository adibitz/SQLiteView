"""SQL code editor: line numbers, highlighting, schema-aware completion, error marks, find bar.

Replaces upstream's QScintilla based ``ExtendedScintilla``/``SqlTextEdit``/``SqlUiLexer``.
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import QRect, QRegularExpression, QSize, QStringListModel, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QKeySequence, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextCursor,
                           QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QCompleter, QHBoxLayout, QLabel, QPlainTextEdit, QTextEdit, QToolButton, QVBoxLayout,
                               QWidget)

from ..core.settings import Settings
from .theme import mono_font, theme, tokens
from .widgets import SearchLineEdit, tool_button

SQL_KEYWORDS = """ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE BEGIN BETWEEN BY
CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME
CURRENT_TIMESTAMP DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH ELSE END ESCAPE EXCEPT
EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM FULL GENERATED GLOB GROUP GROUPS HAVING IF
IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT INTO IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT
MATCH MATERIALIZED NATURAL NO NOT NOTHING NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN
PRAGMA PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME REPLACE RESTRICT RETURNING
RIGHT ROLLBACK ROW ROWID ROWS SAVEPOINT SELECT SET STORED STRICT TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER
UNBOUNDED UNION UNIQUE UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT
INTEGER TEXT BLOB REAL NUMERIC TRUE FALSE""".split()

SQL_FUNCTIONS = """abs avg changes char coalesce count date datetime glob group_concat hex ifnull iif instr json json_array
json_extract json_group_array json_group_object json_insert json_object json_patch json_remove json_replace json_set
json_type json_valid json_each json_tree julianday last_insert_rowid length like likelihood likely lower ltrim max min
nullif printf quote random randomblob replace round rtrim sign soundex sqlite_version strftime substr substring sum
total total_changes trim typeof unicode unixepoch upper zeroblob row_number rank dense_rank percent_rank cume_dist ntile
lag lead first_value last_value nth_value format concat concat_ws octet_length acos asin atan ceil cos degrees exp floor
ln log log2 log10 mod pi pow power radians sin sqrt tan trunc time timediff""".split()


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, doc: QTextDocument):
        super().__init__(doc)
        self.table_names: set[str] = set()
        self._build_rules()

    def _fmt(self, color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.DemiBold)
        f.setFontItalic(italic)
        return f

    def _build_rules(self) -> None:
        t = tokens()
        self.f_kw = self._fmt(t.syn_keyword, bold=True)
        self.f_fn = self._fmt(t.syn_function)
        self.f_tbl = self._fmt(t.syn_table)
        self.f_str = self._fmt(t.syn_string)
        self.f_num = self._fmt(t.syn_number)
        self.f_com = self._fmt(t.syn_comment, italic=True)
        self.f_ident = self._fmt(t.syn_table)
        self.re_word = QRegularExpression(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
        self.re_num = QRegularExpression(r"\b(0[xX][0-9a-fA-F]+|\d+(\.\d*)?([eE][+-]?\d+)?)\b")
        self.kw = {k.upper() for k in SQL_KEYWORDS}
        self.fn = {f.lower() for f in SQL_FUNCTIONS}

    def refresh_theme(self) -> None:
        self._build_rules()
        self.rehighlight()

    def set_tables(self, names: set[str]) -> None:
        self.table_names = {n.lower() for n in names}
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        # words
        it = self.re_word.globalMatch(text)
        while it.hasNext():
            m = it.next()
            w = m.captured(0)
            if w.upper() in self.kw:
                self.setFormat(m.capturedStart(), m.capturedLength(), self.f_kw)
            elif w.lower() in self.fn:
                nxt = text[m.capturedEnd():m.capturedEnd() + 1]
                if nxt == "(":
                    self.setFormat(m.capturedStart(), m.capturedLength(), self.f_fn)
            elif w.lower() in self.table_names:
                self.setFormat(m.capturedStart(), m.capturedLength(), self.f_tbl)
        it = self.re_num.globalMatch(text)
        while it.hasNext():
            m = it.next()
            self.setFormat(m.capturedStart(), m.capturedLength(), self.f_num)
        # strings, quoted identifiers and comments with block state: 1 = in /* */, 2 = in '...', 3 = in "..."
        state = self.previousBlockState()
        i, n = 0, len(text)
        start = 0
        if state in (1, 2, 3):
            start = 0
        else:
            state = 0
        while i < n:
            c = text[i]
            if state == 0:
                if c == "-" and text.startswith("--", i):
                    self.setFormat(i, n - i, self.f_com)
                    break
                if c == "/" and text.startswith("/*", i):
                    state, start = 1, i
                    i += 2
                    continue
                if c == "'":
                    state, start = 2, i
                elif c in "\"`":
                    state, start = 3, i
                    self._quote = c
                elif c == "[":
                    j = text.find("]", i)
                    j = n - 1 if j < 0 else j
                    self.setFormat(i, j - i + 1, self.f_ident)
                    i = j
            elif state == 1:
                if c == "*" and text.startswith("*/", i):
                    self.setFormat(start, i + 2 - start, self.f_com)
                    state = 0
                    i += 2
                    continue
            elif state == 2:
                if c == "'":
                    if text.startswith("''", i):
                        i += 2
                        continue
                    self.setFormat(start, i + 1 - start, self.f_str)
                    state = 0
            elif state == 3:
                if c == getattr(self, "_quote", '"'):
                    self.setFormat(start, i + 1 - start, self.f_ident)
                    state = 0
            i += 1
        if state == 1:
            self.setFormat(start, n - start, self.f_com)
        elif state == 2:
            self.setFormat(start, n - start, self.f_str)
        elif state == 3:
            self.setFormat(start, n - start, self.f_ident)
            state = 0  # identifiers can't span lines
        self.setCurrentBlockState(state)


class _Gutter(QWidget):
    def __init__(self, editor: "SqlEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_gutter(event)


class SqlEditor(QPlainTextEdit):
    """Plain text SQL editor with IDE conveniences."""

    executeRequested = Signal()

    def __init__(self, parent=None, read_only: bool = False):
        super().__init__(parent)
        self.setProperty("role", "editor")
        self.setReadOnly(read_only)
        self._gutter = _Gutter(self)
        self.highlighter = SqlHighlighter(self.document())
        self._error_range: Optional[tuple[int, int]] = None
        self._exec_range: Optional[tuple[int, int]] = None
        self._schema_words: list[str] = []
        self._columns_by_table: dict[str, list[str]] = {}
        self.apply_settings()
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._update_extra_selections)
        self.textChanged.connect(self._clear_error_on_edit)
        self._completer = QCompleter(self)
        self._completer.setWidget(self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.setModelSorting(QCompleter.CaseInsensitivelySortedModel)
        self._completer.activated.connect(self._insert_completion)
        self._completer_model = QStringListModel(self)
        self._completer.setModel(self._completer_model)
        self._update_gutter_width()
        theme.changed.connect(self._on_theme)

    # ------------------------------------------------------------------ setup
    def apply_settings(self) -> None:
        f = mono_font(int(Settings.get("editor", "fontsize")))
        self.setFont(f)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * int(Settings.get("editor", "tabsize")))
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if Settings.get("editor", "wrap_lines") else QPlainTextEdit.NoWrap)
        self._update_gutter_width()

    def _on_theme(self) -> None:
        self.highlighter.refresh_theme()
        self._update_extra_selections()

    def set_schema(self, tables: dict[str, list[str]]) -> None:
        """Provide table -> column names for highlighting and completion."""
        self._columns_by_table = {k.lower(): v for k, v in tables.items()}
        words = set(tables)
        for cols in tables.values():
            words.update(cols)
        self._schema_words = sorted(words, key=str.lower)
        self.highlighter.set_tables(set(tables))

    # ------------------------------------------------------------------ gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event) -> None:
        t = tokens()
        p = QPainter(self._gutter)
        p.fillRect(event.rect(), QColor(t.surface))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        err_block = self.document().findBlock(self._error_range[0]).blockNumber() if self._error_range else -1
        p.setFont(self.font())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                color = t.danger if num == err_block else (t.text if num == cur else t.text_faint)
                p.setPen(QColor(color))
                p.drawText(0, top, self._gutter.width() - 8, self.fontMetrics().height(), Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1
        p.end()

    # ------------------------------------------------------------------ highlights
    def _update_extra_selections(self) -> None:
        t = tokens()
        sels = []
        if not self.isReadOnly():
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(t.syn_current_line))
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            s.cursor = self.textCursor()
            s.cursor.clearSelection()
            sels.append(s)
        if self._exec_range:
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(t.accent_soft))
            c = QTextCursor(self.document())
            c.setPosition(self._exec_range[0])
            c.setPosition(min(self._exec_range[1], len(self.toPlainText())), QTextCursor.KeepAnchor)
            s.cursor = c
            sels.append(s)
        if self._error_range and Settings.get("editor", "error_indicators"):
            s = QTextEdit.ExtraSelection()
            s.format.setUnderlineStyle(QTextCharFormat.WaveUnderline)
            s.format.setUnderlineColor(QColor(t.danger))
            s.format.setBackground(QColor(t.danger_soft))
            c = QTextCursor(self.document())
            c.setPosition(self._error_range[0])
            c.setPosition(min(self._error_range[1], len(self.toPlainText())), QTextCursor.KeepAnchor)
            s.cursor = c
            sels.append(s)
        self.setExtraSelections(sels)

    def set_error_range(self, start: int, end: int) -> None:
        self._error_range = (start, end)
        self._update_extra_selections()
        self._gutter.update()

    def set_executing_range(self, start: int, end: int) -> None:
        self._exec_range = (start, end)
        self._update_extra_selections()

    def clear_marks(self) -> None:
        self._error_range = None
        self._exec_range = None
        self._update_extra_selections()
        self._gutter.update()

    def _clear_error_on_edit(self) -> None:
        if self._error_range or self._exec_range:
            self.clear_marks()

    # ------------------------------------------------------------------ editing helpers
    def current_line_range(self) -> tuple[int, int]:
        b = self.textCursor().block()
        return b.position(), b.position() + b.length() - 1

    def selection_range(self) -> Optional[tuple[int, int]]:
        c = self.textCursor()
        if not c.hasSelection():
            return None
        return c.selectionStart(), c.selectionEnd()

    def toggle_comment(self) -> None:
        c = self.textCursor()
        doc = self.document()
        start_block = doc.findBlock(c.selectionStart())
        end_block = doc.findBlock(c.selectionEnd() - (1 if c.hasSelection() and c.selectionEnd() > c.selectionStart() else 0))
        blocks = []
        b = start_block
        while b.isValid():
            blocks.append(b)
            if b == end_block:
                break
            b = b.next()
        all_commented = all(bl.text().lstrip().startswith("--") or not bl.text().strip() for bl in blocks)
        c.beginEditBlock()
        for bl in blocks:
            text = bl.text()
            cc = QTextCursor(bl)
            if all_commented:
                idx = text.find("--")
                if idx >= 0:
                    cc.setPosition(bl.position() + idx)
                    cc.setPosition(bl.position() + idx + (3 if text[idx + 2:idx + 3] == " " else 2), QTextCursor.KeepAnchor)
                    cc.removeSelectedText()
            elif text.strip():
                indent = len(text) - len(text.lstrip())
                cc.setPosition(bl.position() + indent)
                cc.insertText("-- ")
        c.endEditBlock()

    def keyPressEvent(self, e) -> None:
        popup = self._completer.popup()
        if popup.isVisible() and e.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab, Qt.Key_Escape, Qt.Key_Backtab):
            if e.key() == Qt.Key_Escape:
                popup.hide()
                return
            e.ignore()
            return
        if e.matches(QKeySequence.StandardKey.Find):
            fb = self.parent().findChild(FindBar) if self.parent() else None
            if fb is not None:
                fb.open_for(self)
                return
        if e.key() == Qt.Key_Slash and e.modifiers() & Qt.ControlModifier:
            self.toggle_comment()
            return
        if e.key() == Qt.Key_Tab and not e.modifiers():
            c = self.textCursor()
            if c.hasSelection():
                self._indent_selection(True)
            else:
                c.insertText(" " * int(Settings.get("editor", "tabsize")))
            return
        if e.key() == Qt.Key_Backtab:
            self._indent_selection(False)
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
            c = self.textCursor()
            text = c.block().text()
            indent = text[: len(text) - len(text.lstrip())]
            super().keyPressEvent(e)
            self.textCursor().insertText(indent)
            return
        if e.key() == Qt.Key_Space and e.modifiers() & Qt.ControlModifier:
            self._show_completions(force=True)
            return
        super().keyPressEvent(e)
        if Settings.get("editor", "auto_completion") and not self.isReadOnly():
            txt = e.text()
            if txt and (txt.isalnum() or txt in "_."):
                self._show_completions()
            elif popup.isVisible():
                popup.hide()

    def _indent_selection(self, indent: bool) -> None:
        c = self.textCursor()
        doc = self.document()
        size = int(Settings.get("editor", "tabsize"))
        b = doc.findBlock(c.selectionStart())
        end = doc.findBlock(max(c.selectionStart(), c.selectionEnd() - 1))
        c.beginEditBlock()
        while b.isValid():
            cc = QTextCursor(b)
            if indent:
                cc.insertText(" " * size)
            else:
                text = b.text()
                n = min(size, len(text) - len(text.lstrip(" ")))
                cc.setPosition(b.position() + n, QTextCursor.KeepAnchor)
                cc.removeSelectedText()
            if b == end:
                break
            b = b.next()
        c.endEditBlock()

    # ------------------------------------------------------------------ completion
    def _word_before_cursor(self) -> tuple[str, str]:
        c = self.textCursor()
        block_text = c.block().text()[: c.positionInBlock()]
        m = re.search(r'(?:([A-Za-z_][\w]*|"[^"]+")\.)?([A-Za-z_][\w]*)?$', block_text)
        if not m:
            return "", ""
        return (m.group(1) or "").strip('"'), m.group(2) or ""

    def _show_completions(self, force: bool = False) -> None:
        qualifier, prefix = self._word_before_cursor()
        if qualifier:
            words = self._columns_by_table.get(qualifier.lower(), [])
        else:
            if len(prefix) < 2 and not force:
                self._completer.popup().hide()
                return
            upper = Settings.get("editor", "upper_keywords")
            words = [k if upper else k.lower() for k in SQL_KEYWORDS] + [f + "(" for f in SQL_FUNCTIONS] + self._schema_words
        if not words:
            return
        self._completer_model.setStringList(sorted(set(words), key=str.lower))
        self._completer.setCompletionPrefix(prefix)
        if self._completer.completionCount() == 0 or (self._completer.completionCount() == 1 and
                                                     self._completer.currentCompletion() == prefix):
            self._completer.popup().hide()
            return
        self._completer.popup().setCurrentIndex(self._completer.completionModel().index(0, 0))
        r = self.cursorRect()
        r.setWidth(self._completer.popup().sizeHintForColumn(0) + self._completer.popup().verticalScrollBar().sizeHint().width() + 24)
        r.translate(self.viewportMargins().left(), 4)
        self._completer.complete(r)

    def _insert_completion(self, word: str) -> None:
        _, prefix = self._word_before_cursor()
        c = self.textCursor()
        c.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(prefix))
        needs_quote = not re.fullmatch(r"[A-Za-z_][\w]*\(?", word)
        c.insertText(f'"{word}"' if needs_quote else word)
        self.setTextCursor(c)


class FindBar(QWidget):
    """Inline find/replace bar shown above an editor (replaces FindReplaceDialog)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.editor: Optional[QPlainTextEdit] = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(4)
        self.find_edit = SearchLineEdit("Find")
        self.find_edit.setFixedWidth(220)
        self.replace_edit = SearchLineEdit("Replace with")
        self.replace_edit.setFixedWidth(200)
        self.status = QLabel()
        self.status.setProperty("role", "faint")
        self.case = tool_button("text", "Match case", checkable=True)
        self.case.setText("Aa")
        self.case.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.regex = tool_button("text", "Regular expression", checkable=True)
        self.regex.setText(".*")
        self.regex.setToolButtonStyle(Qt.ToolButtonTextOnly)
        lay.addWidget(self.find_edit)
        lay.addWidget(self.case)
        lay.addWidget(self.regex)
        lay.addWidget(tool_button("arrow-up", "Previous match", on_click=lambda: self.find(backward=True)))
        lay.addWidget(tool_button("arrow-down", "Next match", on_click=lambda: self.find()))
        lay.addWidget(self.replace_edit)
        b1 = QToolButton()
        b1.setText("Replace")
        b1.clicked.connect(self.replace_one)
        b2 = QToolButton()
        b2.setText("All")
        b2.clicked.connect(self.replace_all)
        lay.addWidget(b1)
        lay.addWidget(b2)
        lay.addWidget(self.status, 1)
        lay.addWidget(tool_button("x", "Close", on_click=self.close_bar))
        self.find_edit.returnPressed.connect(self.find)
        self.find_edit.escapePressed.connect(self.close_bar)
        self.replace_edit.escapePressed.connect(self.close_bar)
        self.find_edit.textChanged.connect(lambda: self.find(from_start=True))
        self.hide()

    def open_for(self, editor: QPlainTextEdit) -> None:
        self.editor = editor
        sel = editor.textCursor().selectedText()
        if sel and " " not in sel:
            self.find_edit.setText(sel)
        self.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def close_bar(self) -> None:
        self.hide()
        if self.editor:
            self.editor.setFocus()

    def _flags(self, backward: bool) -> QTextDocument.FindFlag:
        f = QTextDocument.FindFlag(0)
        if backward:
            f |= QTextDocument.FindBackward
        if self.case.isChecked():
            f |= QTextDocument.FindCaseSensitively
        return f

    def find(self, backward: bool = False, from_start: bool = False) -> bool:
        if self.editor is None or not self.find_edit.text():
            self.status.setText("")
            return False
        doc = self.editor.document()
        cur = self.editor.textCursor()
        if from_start:
            cur.setPosition(cur.selectionStart())
        needle = QRegularExpression(self.find_edit.text()) if self.regex.isChecked() else self.find_edit.text()
        if self.regex.isChecked() and not self.case.isChecked():
            needle.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
        found = doc.find(needle, cur, self._flags(backward))
        if found.isNull():
            wrap = QTextCursor(doc)
            if backward:
                wrap.movePosition(QTextCursor.End)
            found = doc.find(needle, wrap, self._flags(backward))
        if found.isNull():
            self.status.setText("No results")
            return False
        self.status.setText("")
        self.editor.setTextCursor(found)
        return True

    def replace_one(self) -> None:
        if self.editor is None or self.editor.isReadOnly():
            return
        c = self.editor.textCursor()
        if c.hasSelection():
            c.insertText(self.replace_edit.text())
        self.find()

    def replace_all(self) -> None:
        if self.editor is None or self.editor.isReadOnly() or not self.find_edit.text():
            return
        text = self.editor.toPlainText()
        if self.regex.isChecked():
            flags = 0 if self.case.isChecked() else re.I
            try:
                new, n = re.subn(self.find_edit.text(), self.replace_edit.text(), text, flags=flags)
            except re.error as e:
                self.status.setText(str(e))
                return
        else:
            pat = re.compile(re.escape(self.find_edit.text()), 0 if self.case.isChecked() else re.I)
            new, n = pat.subn(lambda _m: self.replace_edit.text(), text)
        if n:
            c = self.editor.textCursor()
            c.beginEditBlock()
            c.select(QTextCursor.Document)
            c.insertText(new)
            c.endEditBlock()
        self.status.setText(f"{n} replaced")


class EditorWithFind(QWidget):
    """An editor plus its find bar."""

    def __init__(self, editor: QPlainTextEdit, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.find_bar = FindBar(self)
        self.editor = editor
        lay.addWidget(self.find_bar)
        lay.addWidget(editor)
        editor.setParent(self)
